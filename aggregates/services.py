# aggregates/services.py
"""
Взвешенные агрегаты игроков/тренеров/команд/судей + fan-bias для веса
голоса и trust_score — единственный источник anti-bias логики в проекте.

2026-08-23, продуктовый запрос "докрутить защиту рейтингов от сговора
фан-базы": до этой даты весь код в этом файле (calculate_user_weight,
compute_bias_score, calculate_weighted_average, сегментация свои/чужие)
существовал только здесь и в тестах (aggregates/tests.py) — реальный
пересчёт агрегатов в проде шёл через ОТДЕЛЬНЫЙ наивный дубль в
aggregates/tasks.py (обычное среднее, без единой защиты). Это было
критической дырой: анти-фрод логика формально "была в проекте", но не
влияла ни на одно реальное число на сайте. aggregates/tasks.py теперь
импортирует и использует функции ИМЕННО отсюда — этот файл единственный
источник правды для формулы агрегата, tasks.py — только batch-upsert
обвязка вокруг него.

calculate_user_weight/build_user_weight_map считают вес пользователя один
раз на матч и переиспользуют — вес не зависит от того, какое поле
(contribution/risk/potential/tactics/...) усредняется, и не зависит от
типа сущности (игрок/тренер/команда/судья) — один и тот же вес
пользователя применяется ко ВСЕМ его оценкам в рамках одного матча.
compute_bias_score — единственный источник исторического bias-score в
проекте (использует и вес голоса, и бейдж bias_free в users/services.py,
каждый со своим порогом интерпретации поверх одного числа); считается
по PlayerEvaluation.contribution (самый богатый сигнал — 22 оценки за
матч), но полученный per-user флаг применяется через calculate_user_weight
и к оценкам команд/тренеров/судей тоже — предвзятый фанат есть предвзятый
фанат независимо от того, что именно он сейчас оценивает.
calculate_user_trust_adjustment считает RMSE отклонений от сообщества
поигроково, а не по среднему за весь матч — усреднение по матчу маскирует
предвзятость (10/10 своим + 1/10 чужим даёт околонулевую разницу средних).

winsorize_values/calculate_weighted_average(winsorize=True) — второй,
НЕЗАВИСИМЫЙ от вычисления веса слой защиты: обрезка хвостов распределения
перед усреднением. Вес ловит ИЗВЕСТНЫХ по истории предвзятых пользователей
(3+ матча) — свежесозданные аккаунты без истории, которых позвали в
соцсетях/телеграм-чате занизить оценку конкретной сущности ПОСЛЕ
конкретного матча, весом не ловятся. Винзоризация защищает структурно,
независимо от того, идентифицирован ли сговор как сигнал (см.
detect_vote_velocity_anomalies_task в aggregates/tasks.py) — работает
даже до/без ручного разбора модератором.

segment_evaluations_by_side — разбиение мнений по лагерю зрителя (свои/
чужие/нейтральные). До 2026-08-23 было read-only (только отображение) —
теперь ТАКЖЕ возвращает число голосов в каждом лагере, и эти счётчики
используются apply_neutral_anchor'ом (см. ниже), то есть сегментация
теперь ВЛИЯЕТ на performance_score игроков/команд/судей, не только на
карточку "доверие к рейтингу".

2026-08-23, ВТОРОЙ РАУНД ("а если не занижать экстремально, а по чуть-чуть
— своим 8-10, чужим 5, систематически?"): жёсткие пороги
(FAN_BIAS_EXTREME_TEAM_SCORE=9 И FAN_BIAS_EXTREME_OPPONENT_SCORE<=3
ОДНОВРЕМЕННО, EXTREME_LOW_MAX/EXTREME_HIGH_MIN в aggregates/tasks.py)
в принципе не видят УМЕРЕННОЕ, но систематическое смещение — 8 против 5
никогда не попадёт под "экстремальное". Продукт прямо попросил защиту от
этого паттерна. Два независимых, дополняющих друг друга механизма:

1. compute_bias_profile/_graduated_bias_penalty — вес голоса пользователя
   штрафуется ГРАДУИРОВАННО (не по щелчку жёсткого порога) пропорционально
   средней исторической разнице "своим минус чужим" (mean_diff), плюс
   доп. множитель, если эта разница ПОДОЗРИТЕЛЬНО СТАБИЛЬНА матч к матчу
   (diff_stdev) — реальный фанат реагирует на ход игры, "механическая"
   накрутка почти не колеблется. Ловит ИЗВЕСТНОГО по истории пользователя
   (3+ матча), как и раньше compute_bias_score/calculate_user_weight, но
   теперь чувствительна к умеренному смещению, а не только к экстремальному.

2. apply_neutral_anchor — структурный механизм НЕЗАВИСИМО от истории
   конкретного пользователя: чем больше доля голосовавших являются
   болельщиками одной из двух сторон матча (own+rival) относительно
   нейтралов, тем сильнее итоговый performance_score утягивается к
   мнению нейтральной аудитории (до NEUTRAL_ANCHOR_MAX_PULL). Симметрично
   давит на ОБЕ пристрастные стороны — работает, даже если фанаты обеих
   команд одновременно играют в "8 своим / 5 чужим", и даже если
   пользователи полностью свежие, без истории (что мимо compute_bias_profile).
"""
from __future__ import annotations

import logging
import math
import uuid
from typing import Iterable

from django.core.cache import cache
from django.db.models import Avg, Q

from evaluations.models import ContextEvaluation, PlayerEvaluation
from users.models import User

logger = logging.getLogger(__name__)

FAN_BIAS_CACHE_TTL = 600  # секунд; fan-bias не меняется чаще, чем раз в 10 минут
FAN_BIAS_MIN_HISTORY_MATCHES = 3
FAN_BIAS_LOOKBACK_MATCHES = 10
FAN_BIAS_EXTREME_TEAM_SCORE = 9
FAN_BIAS_EXTREME_OPPONENT_SCORE = 3
FAN_BIAS_THRESHOLD_RATIO = 0.7

# --- Градуированный штраф веса за УМЕРЕННОЕ, но систематическое смещение ---
# (compute_bias_profile/_graduated_bias_penalty; см. докстринг модуля).
# BIAS_FREE_DIFF — зазор для естественной человеческой пристрастности: чуть
# более тёплая оценка своим, чем чужим, это не манипуляция и не штрафуется.
BIAS_FREE_DIFF = 1.0
# Дальше этого значения штраф больше не растёт — тот же порядок величины,
# что и старый жёсткий разрыв (9 против 3 = разница 6).
BIAS_MAX_DIFF = 7.0
# Потолок штрафа СИЛЬНЕЕ старого фиксированного -0.3: теперь под тем же
# самым потолком должны помещаться и умеренные, и экстремальные случаи,
# а экстремальные не должны наказываться слабее, чем раньше.
BIAS_CONTINUOUS_MAX_PENALTY = 0.5
# Ниже этого стандартного отклонения историческая разница "свои минус
# чужие" считается подозрительно стабильной от матча к матчу — сигнатура
# механической накрутки, а не искренней пристрастности (см. докстринг
# compute_bias_profile).
BIAS_LOW_VARIANCE_STDEV = 1.0
BIAS_LOW_VARIANCE_MULTIPLIER = 1.25

# --- Нейтральный якорь (apply_neutral_anchor) ---
# Меньше этого числа нейтральных голосов — не доверяем нейтральному
# среднему как якорю (слишком шумно на 1-2 голосах).
NEUTRAL_ANCHOR_MIN_VOTES = 3
# Максимум, на сколько итоговый performance_score может быть утянут к
# нейтральному среднему, даже если ВСЕ голосовавшие пристрастны — якорь
# ограничивает влияние фанатских лагерей, но не отменяет его полностью
# (иначе голос болельщиков вообще не имел бы значения).
NEUTRAL_ANCHOR_MAX_PULL = 0.4

# Порог голосов, ниже которого рейтинг игрока не считается статистически
# представительным. Используется и для фильтрации топов (matches/views.py,
# teams/views.py), и для отображения ("Недостаточно данных" —
# core/templatetags/rating_extras.py, импортирует это же значение).
MIN_VOTES_FOR_DISPLAY = 5

# Второй, более высокий порог для градуированного бейджа доверия (продуктовый
# аудит "доверие к рейтингу", 2026-08-21): между MIN_VOTES_FOR_DISPLAY и этим
# числом рейтинг уже показывается как число, но помечается "Есть данные", а
# не "Высокая надёжность" — 5 голосов статистически достаточно, чтобы не
# считаться шумом одного тролля, но недостаточно, чтобы считаться устоявшимся
# консенсусом. Используется только для UI-бейджа (core/templatetags/rating_extras.py
# ::confidence_badge), НЕ влияет на то, показывается ли число вообще —
# за это по-прежнему отвечает MIN_VOTES_FOR_DISPLAY.
CONFIDENT_VOTES_THRESHOLD = 15


def calculate_user_weight(
    user: User, context_eval: ContextEvaluation | None, match=None
) -> float:
    """
    Вес голоса для взвешенного среднего: +0.2 за полный просмотр, +0.2 за
    trust_score, минус ГРАДУИРОВАННЫЙ штраф за историческую предвзятость
    (см. _graduated_bias_penalty/compute_bias_profile) — не жёсткий -0.3
    по щелчку порога, как было до 2026-08-23, а плавно растущий штраф от
    BIAS_FREE_DIFF до BIAS_MAX_DIFF, с потолком BIAS_CONTINUOUS_MAX_PENALTY.
    """
    weight = 1.0
    if context_eval and context_eval.watched_type == "full":
        weight += 0.2
    if user.trust_score > 1.2:
        weight += 0.2
    if match is not None:
        weight -= _graduated_bias_penalty(_bias_profile_cached(user, match))
    return max(0.3, min(2.0, weight))


def compute_bias_profile(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> dict:
    """
    Полная статистика исторической предвзятости пользователя по последним
    `lookback` матчам поддерживаемой команды — ОДИН проход по БД считает
    сразу и старый бинарный сигнал (extreme_ratio), и новый непрерывный
    (mean_diff/diff_stdev), 2026-08-23, продуктовый вопрос "а если по
    чуть-чуть — своим 8-10, чужим 5, систематически, а не в лоб?":

    - extreme_ratio: доля матчей, где team_avg>=9 И opponent_avg<=3
      ОДНОВРЕМЕННО — жёсткий порог, НЕ видит умеренное смещение в принципе
      (8 против 5 никогда сюда не попадёт). Оставлен для обратной
      совместимости (compute_bias_score, бейдж bias_free в users/services.py).
    - mean_diff: средняя разница (team_avg - opponent_avg) по ВСЕМ
      рассмотренным матчам, без требования экстремальности — видит
      умеренное систематическое смещение всегда, как только оно выше
      уровня естественной человеческой пристрастности.
    - diff_stdev: разброс этой разницы от матча к матчу. Искренний фанат
      реагирует на ход конкретной игры (проиграли — не поставит 9 своим),
      поэтому его diff колеблется. Подозрительно низкий diff_stdev при
      повышенном mean_diff — сигнатура "механического" паттерна (всегда
      примерно одно и то же число вне зависимости от игры), самостоятельный
      усиливающий сигнал в _graduated_bias_penalty.

    :return: {"considered": int, "extreme_ratio": float,
              "mean_diff": float | None, "diff_stdev": float | None}
        mean_diff/diff_stdev — None при недостатке истории
        (considered < FAN_BIAS_MIN_HISTORY_MATCHES) — это НЕ значит "не
        предвзят", просто нет данных; интерпретацию задаёт вызывающий код.
    """
    empty = {"considered": 0, "extreme_ratio": 0.0, "mean_diff": None, "diff_stdev": None}

    context = (
        ContextEvaluation.objects.filter(user=user, match=match)
        .only("supported_team_id")
        .first()
    )
    supported_team_id = context.supported_team_id if context else None
    if not supported_team_id:
        return empty

    recent_match_ids = list(
        match.__class__.objects.filter(
            Q(home_team_id=supported_team_id) | Q(away_team_id=supported_team_id),
            status="finished",
        )
        .order_by("-start_time")
        .values_list("id", flat=True)[:lookback]
    )

    if len(recent_match_ids) < FAN_BIAS_MIN_HISTORY_MATCHES:
        return empty

    per_match_stats = (
        PlayerEvaluation.objects.filter(user=user, match_id__in=recent_match_ids)
        .values("match_id")
        .annotate(
            team_avg=Avg("contribution", filter=Q(player__team_id=supported_team_id)),
            opponent_avg=Avg(
                "contribution", filter=~Q(player__team_id=supported_team_id)
            ),
        )
    )

    diffs: list[float] = []
    extreme_matches = 0
    for row in per_match_stats:
        if row["team_avg"] is None or row["opponent_avg"] is None:
            continue
        diffs.append(row["team_avg"] - row["opponent_avg"])
        if (
            row["team_avg"] >= FAN_BIAS_EXTREME_TEAM_SCORE
            and row["opponent_avg"] <= FAN_BIAS_EXTREME_OPPONENT_SCORE
        ):
            extreme_matches += 1

    considered = len(diffs)
    if considered < FAN_BIAS_MIN_HISTORY_MATCHES:
        return empty

    return {
        "considered": considered,
        "extreme_ratio": extreme_matches / considered,
        "mean_diff": sum(diffs) / considered,
        "diff_stdev": calculate_std_dev(diffs),
    }


def compute_bias_score(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> float:
    """
    Обратно совместимая обёртка над compute_bias_profile — только
    extreme_ratio. Используется users/services.py::_maybe_award_bias_free
    (бейдж "bias_free") и историческим порогом FAN_BIAS_THRESHOLD_RATIO.
    Для полной статистики (включая непрерывный mean_diff) используйте
    compute_bias_profile напрямую.
    """
    return compute_bias_profile(user, match, lookback)["extreme_ratio"]


def _bias_profile_cached(user: User, match) -> dict:
    """
    compute_bias_profile с кэшем на FAN_BIAS_CACHE_TTL — не пересчитывать
    на каждого игрока/тренера/команду/судью матча, и не по разу на
    каждый ТИП сущности при полном пересчёте (recalculate_all_aggregates_for_match
    ставит 4 отдельные Celery-задачи, каждая строит свой weight_map).
    """
    cache_key = f"fan_bias_profile:{user.id}:{match.id}"
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return cached_value

    profile = compute_bias_profile(user, match)
    cache.set(cache_key, profile, timeout=FAN_BIAS_CACHE_TTL)
    return profile


def _graduated_bias_penalty(profile: dict) -> float:
    """
    Штраф веса голоса, ГРАДУИРОВАННО растущий с mean_diff — прямой ответ
    на продуктовый вопрос "а если не занижать экстремально, а по чуть-чуть,
    своим 8-10, чужим 5, систематически?" (2026-08-23). До этой правки
    единственным сигналом был extreme_ratio>=0.7 → фиксированный -0.3:
    паттерн team_avg=8/opponent_avg=5 (diff=3) НИКОГДА не пересекал
    жёсткий порог team_avg>=9 И opponent_avg<=3 — штраф был 0.0, вне
    зависимости от того, сколько матчей подряд человек так голосовал.

    Теперь: diff > BIAS_FREE_DIFF уже штрафуется, пропорционально
    расстоянию до BIAS_MAX_DIFF (тот же порядок величины, что у старого
    жёсткого разрыва 9-3=6). При diff=3 (пример из вопроса) штраф —
    заметная часть потолка, а не ноль. При по-настоящему экстремальном
    diff штраф выходит на потолок BIAS_CONTINUOUS_MAX_PENALTY=0.5 —
    СИЛЬНЕЕ старых -0.3: экстремальный случай не должен наказываться
    слабее умеренного просто потому, что порог стал непрерывным.

    Доп. множитель BIAS_LOW_VARIANCE_MULTIPLIER — если diff почти не
    колеблется от матча к матчу (diff_stdev < BIAS_LOW_VARIANCE_STDEV),
    это подозрительно: искренняя пристрастность реагирует на ход игры
    (проиграли — не поставишь 9 своим), а "механическая" накрутка ставит
    примерно одно и то же число вне зависимости от результата.
    """
    mean_diff = profile.get("mean_diff")
    if mean_diff is None or mean_diff <= BIAS_FREE_DIFF:
        return 0.0

    span = BIAS_MAX_DIFF - BIAS_FREE_DIFF
    fraction = min(1.0, (mean_diff - BIAS_FREE_DIFF) / span)
    penalty = BIAS_CONTINUOUS_MAX_PENALTY * fraction

    diff_stdev = profile.get("diff_stdev")
    if diff_stdev is not None and diff_stdev < BIAS_LOW_VARIANCE_STDEV:
        penalty *= BIAS_LOW_VARIANCE_MULTIPLIER

    return min(penalty, BIAS_CONTINUOUS_MAX_PENALTY * BIAS_LOW_VARIANCE_MULTIPLIER)


def build_user_weight_map(evaluations: list[PlayerEvaluation], match) -> dict[uuid.UUID, float]:
    """Карта {user_id: вес}, посчитанная один раз на матч — вес не зависит от усредняемого поля."""
    unique_user_ids = {e.user_id for e in evaluations}

    # Контексты просмотра всех зрителей — одним IN-запросом, не по одному на пользователя.
    context_map: dict[uuid.UUID, ContextEvaluation] = {
        ce.user_id: ce
        for ce in ContextEvaluation.objects.filter(
            match_id=match.id, user_id__in=unique_user_ids
        ).only("user_id", "watched_type")
    }

    weight_map: dict[uuid.UUID, float] = {}
    seen_users: set[uuid.UUID] = set()
    for eval_obj in evaluations:
        if eval_obj.user_id in seen_users:
            continue
        seen_users.add(eval_obj.user_id)
        context = context_map.get(eval_obj.user_id)
        weight_map[eval_obj.user_id] = calculate_user_weight(eval_obj.user, context, match)

    return weight_map


def winsorize_values(values: list[float], pct: float = 0.1, min_n: int = 10) -> list[float]:
    """
    Винзоризация — обрезка ХВОСТОВ распределения по перцентилям, а не
    исключение выбросов (в отличие от trimmed mean, ни один голос не
    выбрасывается: экстремальное значение подрезается до границы, но
    продолжает тянуть среднее в свою сторону — просто не может утащить
    его в крайность в одиночку или малой скоординированной группой).

    Технический бэкстоп именно против ОРГАНИЗОВАННОГО СГОВОРА фан-базы
    (продуктовый запрос 2026-08-23: "клуб — большая организация с фан-базой,
    которая может по сговору обрушить рейтинги"). compute_bias_score/
    calculate_user_weight ловят только пользователей с ИЗВЕСТНОЙ ИСТОРИЕЙ
    предвзятости (3+ матча) или уже засвеченных в одном IP-кластере — ни
    то, ни другое не поймает свежую волну РЕАЛЬНЫХ людей с разных IP и
    без истории, которых позвали в соцсетях/телеграм-чате занизить оценку
    конкретному игроку/команде/тренеру/судье ПОСЛЕ конкретного матча.
    Винзоризация защищает независимо от того, поймали мы сговор как
    сигнал (detect_vote_velocity_anomalies_task) или нет — структурно
    ограничивает влияние хвоста ещё до любого ручного разбора.

    :param pct: доля с КАЖДОЙ стороны, попадающая под обрезку (0.1 —
        обрезка 10-го/90-го перцентиля). Умышленно не расширяем дальше:
        искренний резко негативный консенсус БОЛЬШИНСТВА (например, после
        реально провальной игры) — это не сговор, а сигнал, его глушить
        нельзя, обрезаются только хвосты.
    :param min_n: при выборке меньше этого числа винзоризация не
        применяется — на 3-5 голосах перцентили статистически бессмысленны,
        единственная защита на этом объёме — вес пользователя.
    """
    n = len(values)
    if n < min_n:
        return list(values)
    sorted_values = sorted(values)
    lower_idx = min(int(math.floor(n * pct)), n - 1)
    upper_idx = max(int(math.ceil(n * (1 - pct))) - 1, lower_idx)
    upper_idx = min(upper_idx, n - 1)
    lower_bound = sorted_values[lower_idx]
    upper_bound = sorted_values[upper_idx]
    return [min(max(v, lower_bound), upper_bound) for v in values]


def calculate_weighted_average(
    evaluations: list[PlayerEvaluation],
    field_name: str,
    weight_map: dict[uuid.UUID, float],
    winsorize: bool = True,
    winsorize_pct: float = 0.1,
) -> float:
    """
    Взвешенное среднее по полю, с винзоризацией хвостов по умолчанию.

    :param evaluations: материализованный список, не queryset — иначе SQL
        переисполняется на каждый вызов (contribution/risk/potential — 3 раза).
    :param weight_map: см. `build_user_weight_map`.
    :param winsorize: см. `winsorize_values` — обрезка хвостов ПЕРЕД
        взвешиванием, отдельный слой защиты от веса пользователя (весом
        ловим ИЗВЕСТНЫХ предвзятых людей, винзоризацией — любой хвост,
        включая свежесозданные аккаунты без истории).
    """
    if not evaluations:
        return 0.0

    raw_values = [getattr(e, field_name, 0) or 0 for e in evaluations]
    values = winsorize_values(raw_values, pct=winsorize_pct) if winsorize else raw_values

    weighted_sum = 0.0
    total_weight = 0.0
    for eval_obj, value in zip(evaluations, values):
        weight = weight_map.get(eval_obj.user_id, 0.5)
        weighted_sum += value * weight
        total_weight += weight

    return weighted_sum / total_weight if total_weight > 0 else 0.0


def calculate_std_dev(values: Iterable[float]) -> float:
    """Стандартное отклонение выборки."""
    values = list(values)
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return math.sqrt(variance)


def segment_evaluations_by_side_multi(
    evaluations: list, value_fields: tuple[str, ...], entity_team_id, match
) -> dict[str, tuple[float | None, float | None, float | None, int, int, int]]:
    """
    Как segment_evaluations_by_side, но считает сегментацию сразу для
    НЕСКОЛЬКИХ полей за один проход по evaluations и один запрос
    ContextEvaluation — 2026-08-23, продуктовый вопрос "а как защитить
    risk/potential игрока и tactics/substitutions/impact тренера от того
    же умеренного смещения, что и contribution?". Классификация "в каком
    лагере голосующий" (свой/чужой/нейтрал) не зависит от того, какое
    поле сейчас усредняем — считать её заново на каждое поле было бы
    N лишних запросов на N полей. Здесь один проход строит классификацию
    один раз и раскладывает значения ВСЕХ полей сразу по нужным вёдрам.

    :return: {field_name: (own_mean, rival_mean, neutral_mean, own_n, rival_n, neutral_n)}
        Пустой результат по каждому полю ((None, None, None, 0, 0, 0)),
        если сегментация невозможна (нет entity_team_id) или нет оценок.
    """
    empty = (None, None, None, 0, 0, 0)
    if not evaluations or not entity_team_id:
        return {f: empty for f in value_fields}

    opponent_team_id = (
        match.away_team_id if match.home_team_id == entity_team_id else match.home_team_id
    )

    user_ids = {e.user_id for e in evaluations}
    supported_team_map: dict[uuid.UUID, uuid.UUID | None] = {
        ce["user_id"]: ce["supported_team_id"]
        for ce in ContextEvaluation.objects.filter(
            match_id=match.id, user_id__in=user_ids
        ).values("user_id", "supported_team_id")
    }

    buckets: dict[str, dict[str, list[float]]] = {
        f: {"own": [], "rival": [], "neutral": []} for f in value_fields
    }
    for eval_obj in evaluations:
        supported_team_id = supported_team_map.get(eval_obj.user_id)
        if supported_team_id == entity_team_id:
            side = "own"
        elif supported_team_id == opponent_team_id:
            side = "rival"
        else:
            side = "neutral"
        for field_name in value_fields:
            value = getattr(eval_obj, field_name, None)
            if not value:
                continue
            buckets[field_name][side].append(value)

    def _mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    return {
        field_name: (
            _mean(side_values["own"]), _mean(side_values["rival"]), _mean(side_values["neutral"]),
            len(side_values["own"]), len(side_values["rival"]), len(side_values["neutral"]),
        )
        for field_name, side_values in buckets.items()
    }


def segment_evaluations_by_side(
    evaluations: list, value_field: str, entity_team_id, match
) -> tuple[float | None, float | None, float | None, int, int, int]:
    """
    ОБОБЩЁННАЯ версия сегментации "свои/чужие/нейтральные" для ОДНОГО поля
    — тонкая обёртка над segment_evaluations_by_side_multi (см. её докстринг
    за полным объяснением). Работает для любого типа оценки, у которой
    есть числовое поле `value_field` и известна команда-владелец
    `entity_team_id` (команда игрока/тренера, либо сама команда — для
    TeamEvaluation).

    До 2026-08-23 было ЧИСТО read-only (только отображение, не влияло на
    performance_score) — среднее внутри лагеря намеренно невзвешенное:
    вес пользователя тут сместил бы интерпретацию "как оценили именно
    фанаты X". Это осталось неизменным. Изменилось другое: теперь ТАКЖЕ
    возвращаются счётчики голосов в каждом лагере (own_n/rival_n/neutral_n)
    — их использует apply_neutral_anchor для итогового performance_score.

    :param entity_team_id: None — сегментация невозможна (например,
        RefereeEvaluation, где нет команды-владельца у самой сущности) —
        возвращает (None, None, None, 0, 0, 0) без запроса к БД.
    :return: (own_mean, rival_mean, neutral_mean, own_n, rival_n, neutral_n)
    """
    return segment_evaluations_by_side_multi(evaluations, (value_field,), entity_team_id, match)[
        value_field
    ]


def _segment_by_fan_side(
    evaluations: list[PlayerEvaluation], player, match
) -> tuple[float | None, float | None, float | None, int, int, int]:
    """Player-специфичная обёртка над segment_evaluations_by_side — сохранена
    ради обратной совместимости (recalculate_player_aggregate и тесты)."""
    return segment_evaluations_by_side(evaluations, "contribution", player.team_id, match)


def apply_neutral_anchor(
    pooled_score: float,
    neutral_avg: float | None,
    own_n: int,
    rival_n: int,
    neutral_n: int,
) -> float:
    """
    Структурная защита от УМЕРЕННОГО, но систематического смещения ОБЕИХ
    пристрастных сторон — 2026-08-23, продуктовый вопрос "а если своим
    8-10, чужим 5, и так с обеих сторон дерби?". В отличие от
    _graduated_bias_penalty (штрафует ИЗВЕСТНОГО по истории пользователя),
    этот механизм не требует истории вообще — работает и на свежих
    аккаунтах без единого прошлого голоса, если рядом есть нейтральная
    аудитория, с которой можно сверить пристрастный консенсус.

    Идея: чем больше доля голосовавших являются болельщиками ОДНОЙ из
    двух сторон конкретного матча (own+rival) относительно нейтралов, тем
    сильнее итоговый score утягивается к среднему нейтралов — до
    NEUTRAL_ANCHOR_MAX_PULL. При партизанской доле, близкой к 0
    (голосовали почти одни нейтралы), pull около нуля — pooled_score и
    так уже достаточно надёжен без коррекции. При доле около 1 (голосуют
    почти одни партизаны с обеих сторон — типичная картина дерби) якорь
    тянет к нейтральному мнению почти на полную мощность.

    Симметричен по конструкции: не важно, кто именно смещает оценку
    "вверх" (свои фанаты сущности) или "вниз" (фанаты соперника) — обе
    силы одинаково размываются нейтральным якорем, поэтому механизм не
    даёт преимущества и не наказывает конкретно одну из сторон дерби.

    :param pooled_score: уже взвешенное и винзоризованное среднее
        (calculate_weighted_average) — вход для коррекции, не замена.
    :param neutral_avg: среднее нейтрального лагеря (segment_evaluations_by_side).
        None или недостаточно голосов (NEUTRAL_ANCHOR_MIN_VOTES) — коррекция
        не применяется: нет надёжного независимого ориентира, лучше не
        гадать, чем внести шум с 1-2 нейтральных голосов.
    """
    if neutral_avg is None or neutral_n < NEUTRAL_ANCHOR_MIN_VOTES:
        return pooled_score

    total = own_n + rival_n + neutral_n
    if total == 0:
        return pooled_score

    partisan_share = (own_n + rival_n) / total
    pull = NEUTRAL_ANCHOR_MAX_PULL * partisan_share
    return pooled_score * (1 - pull) + neutral_avg * pull


def recalculate_player_aggregate(player, match):
    """Пересчёт агрегатов игрока за матч с учётом весов. Всегда возвращает PlayerMatchAggregate (или None без оценок)."""
    from .models import MatchAggregate, PlayerMatchAggregate

    match_id = str(match.id)

    evaluations = list(
        PlayerEvaluation.objects.filter(player=player, match=match).select_related("user")
    )
    if not evaluations:
        return None

    weight_map = build_user_weight_map(evaluations, match)

    avg_contribution = calculate_weighted_average(evaluations, "contribution", weight_map)
    avg_risk = calculate_weighted_average(evaluations, "risk", weight_map)
    avg_potential = calculate_weighted_average(evaluations, "potential", weight_map)

    # Один проход/один запрос сегментирует СРАЗУ contribution И risk —
    # риск защищаем тем же нейтральным якорем, что и вклад (2026-08-23):
    # "Игрок на грани" (номинация по risk_index) — репутационно самая
    # чувствительная негативная номинация на сайте, её нельзя оставлять
    # без защиты только потому, что исторически защитили только contribution.
    segments = segment_evaluations_by_side_multi(
        evaluations, ("contribution", "risk"), player.team_id, match
    )
    own_fans_avg, rival_fans_avg, neutral_avg, own_n, rival_n, neutral_n = segments["contribution"]
    _, _, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n = segments["risk"]

    contributions = [e.contribution for e in evaluations if e.contribution]
    std_dev = calculate_std_dev(contributions)
    stability_index = 1.0 / std_dev if std_dev > 0 else 10.0

    drama_index = cache.get(f"match_agg_{match_id}")
    if drama_index is None:
        match_agg = MatchAggregate.objects.filter(match=match).only("drama_index").first()
        drama_index = match_agg.drama_index if match_agg else 5.0
        cache.set(f"match_agg_{match_id}", drama_index, 600)

    # performance_score/risk_index, в отличие от avg_contribution/avg_risk,
    # дополнительно утянуты к нейтральному якорю (apply_neutral_anchor) при
    # высокой доле пристрастных голосов — avg_contribution/avg_risk
    # остаются "сырыми" взвешенными средними для прозрачности (видно,
    # НАСКОЛЬКО якорь скорректировал итог).
    performance_score = apply_neutral_anchor(avg_contribution, neutral_avg, own_n, rival_n, neutral_n)
    risk_index_value = apply_neutral_anchor(
        avg_risk, neutral_risk_avg, risk_own_n, risk_rival_n, risk_neutral_n
    )
    maturity_score = performance_score - risk_index_value
    clutch_index = performance_score * (drama_index / 10.0)

    aggregate, _created = PlayerMatchAggregate.objects.update_or_create(
        player=player,
        match=match,
        defaults={
            "avg_contribution": round(avg_contribution, 2),
            "avg_risk": round(avg_risk, 2),
            "avg_potential": round(avg_potential, 2),
            "total_votes": len(evaluations),
            "performance_score": round(performance_score, 2),
            "risk_index": round(risk_index_value, 2),
            "maturity_score": round(maturity_score, 2),
            "stability_index": round(stability_index, 2),
            "clutch_index": round(clutch_index, 2),
            "own_fans_avg": round(own_fans_avg, 2) if own_fans_avg is not None else None,
            "rival_fans_avg": round(rival_fans_avg, 2) if rival_fans_avg is not None else None,
            "neutral_avg": round(neutral_avg, 2) if neutral_avg is not None else None,
        },
    )

    cache.set(
        f"player_agg_{player.id}_{match_id}",
        {
            "id": str(aggregate.id),
            "performance_score": aggregate.performance_score,
            "total_votes": aggregate.total_votes,
        },
        300,
    )

    return aggregate


def calculate_user_trust_adjustment(user, match) -> float:
    """
    Корректировка trust_score по точности оценок. RMSE отклонений от среднего
    сообщества считается поигроково и агрегируется — усреднение по всему
    матчу маскирует предвзятость (10/10 своим + 1/10 чужим даёт околонулевую
    разницу средних, хотя каждая оценка предвзята).
    """
    user_evals = list(
        PlayerEvaluation.objects.filter(user=user, match=match).values(
            "player_id", "contribution"
        )
    )
    if not user_evals:
        return 0.0

    player_ids = [e["player_id"] for e in user_evals]

    # exclude(user=user) — иначе пользователь частично сравнивается сам с собой.
    community_avg_by_player: dict[uuid.UUID, float] = {
        row["player_id"]: row["avg"]
        for row in PlayerEvaluation.objects.filter(match=match, player_id__in=player_ids)
        .exclude(user=user)
        .values("player_id")
        .annotate(avg=Avg("contribution"))
    }

    squared_errors = []
    for row in user_evals:
        community_avg = community_avg_by_player.get(row["player_id"])
        if community_avg is None:
            continue  # пользователь — единственный оценивший этого игрока
        squared_errors.append((row["contribution"] - community_avg) ** 2)

    if not squared_errors:
        return 0.0

    rmse = math.sqrt(sum(squared_errors) / len(squared_errors))

    # Нормализация на шкалу 0..1 (максимально возможная ошибка на шкале 1..10 — 9)
    normalized_deviation = min(rmse / 5.0, 1.0)

    if normalized_deviation < 0.3:
        return 0.05  # Адекватный аналитик — стабильно близок к консенсусу по каждому игроку
    if normalized_deviation < 0.6:
        return 0.0  # Норма
    return -0.05  # Систематически предвзят по отдельным игрокам


def detect_fan_bias(user, match, supported_team=None) -> dict:
    """Снимок предвзятости по одному матчу (админка/модерация) — в отличие от compute_bias_score, которая историческая."""
    if not supported_team:
        context = ContextEvaluation.objects.filter(user=user, match=match).first()
        supported_team = context.supported_team if context else None

    if not supported_team:
        return {"is_biased": False, "score": 0.0}

    own_team_evals = (
        PlayerEvaluation.objects.filter(
            user=user, match=match, player__team=supported_team
        ).aggregate(avg=Avg("contribution"))["avg"]
        or 0
    )

    opponent_team = (
        match.away_team if match.home_team_id == supported_team.id else match.home_team
    )
    opponent_evals = (
        PlayerEvaluation.objects.filter(
            user=user, match=match, player__team=opponent_team
        ).aggregate(avg=Avg("contribution"))["avg"]
        or 0
    )

    bias_score = own_team_evals - opponent_evals
    is_biased = bias_score > 4.0

    return {
        "is_biased": is_biased,
        "score": bias_score,
        "own_team_avg": own_team_evals,
        "opponent_avg": opponent_evals,
    }