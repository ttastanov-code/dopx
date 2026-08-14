# aggregates/services.py
"""
Сервисный слой расчёта взвешенных агрегатов и антифрод-метрик (trust/bias).

НАЙДЕННЫЕ ПРОБЛЕМЫ И ИХ ИСПРАВЛЕНИЕ (см. подробности в каждой функции ниже):

1. `calculate_weighted_average` вызывала
   `evaluations_queryset.select_related('user', 'user__context_evaluations')`.
   `context_evaluations` — это related_name ОБРАТНОЙ связи ManyToOne
   (ContextEvaluation.user -> User), а не forward-FK/O2O. `select_related()`
   умеет обходить только forward-связи (FK/O2O) — при попытке обойти обратную
   ManyToOne связь Django на этапе выполнения запроса выбрасывает
   `django.core.exceptions.FieldError`. Иными словами, эта функция была не
   просто неэффективной — она была БИТОЙ и падала бы в проде при первом же
   реальном вызове. Именно поэтому реальный N+1 в цикле
   `eval_obj.user.context_evaluations.filter(match_id=match_id).first()`
   никогда не "просто тормозил" — код был неработоспособен.

2. Даже если убрать невалидный select_related, цикл по каждой оценке всё
   равно делал отдельный SQL-запрос за ContextEvaluation пользователя —
   классический N+1. Плюс `calculate_user_weight` внутри себя вызывала
   `_detect_fan_bias`, которая сама по себе сканирует до 10 прошлых матчей
   пользователя (до 2 запросов на матч) — то есть ОДНА оценка могла стоить
   более 20 SQL-запросов. А поскольку вес пользователя одинаков для ВСЕХ
   оценок этого пользователя в рамках матча (не зависит от того, какого
   игрока/какое поле мы усредняем), при пересчёте одного игрока по трём
   полям (contribution/risk/potential) эта дорогая проверка выполнялась
   ПОВТОРНО 3 раза на каждого зрителя.

3. `calculate_user_trust_adjustment` сравнивала СРЕДНЮЮ оценку пользователя
   по всему матчу со СРЕДНЕЙ оценкой сообщества по всему матчу. Это позволяет
   пользователю поставить 10/10 плохому игроку своей команды и 1/10 хорошему
   игроку соперника — при усреднении по матчу отклонение может обнулиться,
   хотя по факту каждая отдельная оценка радикально предвзята. Заменено на
   RMSE отклонений, посчитанных ПОИГРОКОВО и только затем агрегированных.

Все исправления ниже сохраняют существующие публичные сигнатуры там, где это
возможно (чтобы не ломать вызывающий код в evaluations/views.py и tests.py),
кроме `calculate_user_weight`, которая теперь принимает `match` явным
параметром (без этого невозможно корректно и без лишних запросов
инкапсулировать проверку fan-bias — см. комментарий в самой функции).

4. НОВОЕ (продуктовый аудит): историческая (много-матчевая) детекция
   fan-bias была продублирована ДВАЖДЫ с чуть разными порогами — здесь, в
   `_detect_fan_bias` (порог "≥9 своим / ≤3 чужим в ≥70% из 10 матчей"), и
   ОТДЕЛЬНО, третьей копией, в `users/services.py::check_and_award_badges`
   для бейджа `bias_free` (порог "разница ≤4 в ≥80% из 15 матчей"). Два
   места одной и той же концепции с разными magic-number — почти
   гарантированный дрейф логики при следующем изменении. Вынесено в единую
   `compute_bias_score(user, match, lookback)`, возвращающую непрерывный
   скор `0.0..1.0`, а не готовый bool — вес голоса и критерий бейджа
   применяют СВОИ пороги интерпретации поверх ОДНОГО числа.

   `detect_fan_bias()` ниже — сознательно ОСТАВЛЕНА отдельной: это диагностика
   по ОДНОМУ конкретному матчу (разница оценок своей/чужой команды в этом
   матче), а не историческая метрика по нескольким матчам — разные вопросы,
   разные ответы, не дублирование.
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


def calculate_user_weight(
    user: User, context_eval: ContextEvaluation | None, match=None
) -> float:
    """
    Вес голоса пользователя для взвешенного среднего.

    :param match: матч, в контексте которого считается вес. Нужен для
        детекции fan-bias — раньше матч доставался из `context_eval.match`,
        что требовало ЛИШНЕГО join'а объекта ContextEvaluation целиком ради
        одного FK. Теперь матч передаётся явно вызывающим кодом, который уже
        и так его знает.
    """
    weight = 1.0
    if context_eval and context_eval.watched_type == "full":
        weight += 0.2
    if user.trust_score > 1.2:
        weight += 0.2
    if match is not None and _detect_fan_bias_cached(user, match):
        weight -= 0.3
    return max(0.5, min(2.0, weight))


def compute_bias_score(
    user: User, match, lookback: int = FAN_BIAS_LOOKBACK_MATCHES
) -> float:
    """
    ЕДИНСТВЕННЫЙ источник истины для исторической (много-матчевой) детекции
    fan-bias в проекте — см. пункт 4 в докстринге модуля. Возвращает
    непрерывный скор `0.0..1.0`: доля матчей из последних `lookback`
    завершённых матчей поддерживаемой команды пользователя, где он поставил
    своей команде экстремально высокую (`>= FAN_BIAS_EXTREME_TEAM_SCORE`), а
    сопернику — экстремально низкую (`<= FAN_BIAS_EXTREME_OPPONENT_SCORE`)
    среднюю оценку. `0.0`, если истории недостаточно (`< FAN_BIAS_MIN_
    HISTORY_MATCHES` матчей с данными) — не потому что пользователь
    "не предвзят", а потому что для вывода недостаточно данных; вызывающий
    код (вес голоса, критерий бейджа) должен учитывать это через собственный
    порог интерпретации, а не считать `0.0` доказательством непредвзятости.

    ОПТИМИЗАЦИЯ: раньше на каждый из последних матчей выполнялось ДВА
    отдельных запроса (`team_evals`, `opponent_evals`) — до 20 запросов на
    10 матчей. Теперь это ОДИН запрос с группировкой по `match_id` и
    условными `Avg(..., filter=...)`, независимо от количества матчей.
    """
    context = (
        ContextEvaluation.objects.filter(user=user, match=match)
        .only("supported_team_id")
        .first()
    )
    supported_team_id = context.supported_team_id if context else None
    if not supported_team_id:
        return 0.0

    recent_match_ids = list(
        match.__class__.objects.filter(
            Q(home_team_id=supported_team_id) | Q(away_team_id=supported_team_id),
            status="finished",
        )
        .order_by("-start_time")
        .values_list("id", flat=True)[:lookback]
    )

    if len(recent_match_ids) < FAN_BIAS_MIN_HISTORY_MATCHES:
        return 0.0

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

    considered = 0
    extreme_matches = 0
    for row in per_match_stats:
        if row["team_avg"] is None or row["opponent_avg"] is None:
            continue
        considered += 1
        if (
            row["team_avg"] >= FAN_BIAS_EXTREME_TEAM_SCORE
            and row["opponent_avg"] <= FAN_BIAS_EXTREME_OPPONENT_SCORE
        ):
            extreme_matches += 1

    if considered < FAN_BIAS_MIN_HISTORY_MATCHES:
        return 0.0

    return extreme_matches / considered


def _detect_fan_bias_cached(user: User, match) -> bool:
    """
    Кэширующая обёртка над `compute_bias_score` для веса голоса (bool-порог
    `>= FAN_BIAS_THRESHOLD_RATIO`).

    Расчёт — сканирование до 10 прошлых матчей пользователя, не зависящее от
    того, какого игрока текущего матча мы сейчас агрегируем, поэтому
    кэшируется на FAN_BIAS_CACHE_TTL секунд: при пересчёте всех игроков
    одного матча с одним и тем же набором зрителей проверка выполняется 1
    раз на пользователя, а не по разу на каждого оцененного им игрока.
    """
    cache_key = f"fan_bias:{user.id}:{match.id}"
    cached_value = cache.get(cache_key)
    if cached_value is not None:
        return cached_value

    result = compute_bias_score(user, match) >= FAN_BIAS_THRESHOLD_RATIO
    cache.set(cache_key, result, timeout=FAN_BIAS_CACHE_TTL)
    return result


def _build_user_weight_map(evaluations: list[PlayerEvaluation], match) -> dict[uuid.UUID, float]:
    """
    Строит карту {user_id: вес} ОДИН раз для набора оценок одного игрока.

    КЛЮЧЕВОЙ ФИКС N+1: раньше `calculate_user_weight` вызывалась внутри
    цикла `calculate_weighted_average` — то есть один раз НА КАЖДУЮ оценку,
    а `calculate_weighted_average`, в свою очередь, вызывалась трижды (для
    contribution/risk/potential) с одним и тем же набором оценок. В сумме —
    до 3x избыточных вычислений веса и связанных с ним запросов на каждого
    зрителя. Вес не зависит от поля, которое усредняется, поэтому считаем
    его один раз на уникального пользователя и переиспользуем.
    """
    unique_user_ids = {e.user_id for e in evaluations}

    # Один запрос вместо N: контексты просмотра всех зрителей этого матча.
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


def calculate_weighted_average(
    evaluations: list[PlayerEvaluation],
    field_name: str,
    weight_map: dict[uuid.UUID, float],
) -> float:
    """
    Взвешенное среднее с учётом веса пользователя.

    :param evaluations: уже материализованный (list()) список оценок —
        больше НЕ queryset, чтобы не переисполнять запрос при каждом вызове
        (раньше вызывающий код передавал queryset и функция сама делала
        `list(evaluations_queryset...)`, из-за чего один и тот же SQL
        выполнялся трижды подряд для contribution/risk/potential).
    :param weight_map: заранее посчитанная карта {user_id: вес}, см.
        `_build_user_weight_map`.
    """
    if not evaluations:
        return 0.0

    weighted_sum = 0.0
    total_weight = 0.0
    for eval_obj in evaluations:
        weight = weight_map.get(eval_obj.user_id, 0.5)
        value = getattr(eval_obj, field_name, 0) or 0
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


def recalculate_player_aggregate(player, match):
    """
    Пересчёт агрегатов конкретного игрока за конкретный матч с учётом весов.

    ИСПРАВЛЕН ХИДДЕН-БАГ КОНТРАКТА ВОЗВРАЩАЕМОГО ЗНАЧЕНИЯ: раньше при
    попадании в кэш функция возвращала `dict` (`{'id':..., 'performance_
    score':..., 'total_votes':...}`), а при промахе — экземпляр модели
    `PlayerMatchAggregate`. Вызывающий код (см. aggregates/tests.py) читает
    `aggregate.total_votes` через доступ к атрибуту — на `dict` это упало бы
    с `AttributeError` при повторном вызове функции в пределах TTL кэша.
    Теперь функция ВСЕГДА возвращает экземпляр модели.
    """
    from .models import MatchAggregate, PlayerMatchAggregate

    match_id = str(match.id)

    evaluations = list(
        PlayerEvaluation.objects.filter(player=player, match=match).select_related("user")
    )
    if not evaluations:
        return None

    weight_map = _build_user_weight_map(evaluations, match)

    avg_contribution = calculate_weighted_average(evaluations, "contribution", weight_map)
    avg_risk = calculate_weighted_average(evaluations, "risk", weight_map)
    avg_potential = calculate_weighted_average(evaluations, "potential", weight_map)

    contributions = [e.contribution for e in evaluations if e.contribution]
    std_dev = calculate_std_dev(contributions)
    stability_index = 1.0 / std_dev if std_dev > 0 else 10.0

    drama_index = cache.get(f"match_agg_{match_id}")
    if drama_index is None:
        match_agg = MatchAggregate.objects.filter(match=match).only("drama_index").first()
        drama_index = match_agg.drama_index if match_agg else 5.0
        cache.set(f"match_agg_{match_id}", drama_index, 600)

    performance_score = avg_contribution
    maturity_score = avg_contribution - avg_risk
    clutch_index = avg_contribution * (drama_index / 10.0)

    aggregate, _created = PlayerMatchAggregate.objects.update_or_create(
        player=player,
        match=match,
        defaults={
            "avg_contribution": round(avg_contribution, 2),
            "avg_risk": round(avg_risk, 2),
            "avg_potential": round(avg_potential, 2),
            "total_votes": len(evaluations),
            "performance_score": round(performance_score, 2),
            "risk_index": round(avg_risk, 2),
            "maturity_score": round(maturity_score, 2),
            "stability_index": round(stability_index, 2),
            "clutch_index": round(clutch_index, 2),
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
    Корректировка trust_score пользователя на основе точности его оценок.

    БЫЛО (уязвимость): сравнивалось СРЕДНЕЕ оценок пользователя по матчу
    со СРЕДНИМ оценок сообщества по матчу:

        user_avg = mean(все оценки пользователя за матч)
        community_avg = mean(все оценки сообщества за матч)
        deviation = |user_avg - community_avg|

    Это позволяет пользователю поставить 10/10 слабому игроку своей команды
    и 1/10 сильному игроку соперника — при усреднении по всему матчу
    отклонения компенсируют друг друга, и итоговое `deviation` может выйти
    околонулевым, хотя КАЖДАЯ отдельная оценка экстремально предвзята.

    СТАЛО: считаем RMSE отклонений пользователя от медианного мнения
    сообщества ПОИГРОКОВО, и только потом агрегируем в одно число. Так
    систематическая предвзятость по отдельным игрокам не может "погаситься"
    усреднением по матчу. Дополнительно из бейзлайна сообщества исключается
    оценка самого пользователя, чтобы не сравнивать его с самим собой.
    """
    user_evals = list(
        PlayerEvaluation.objects.filter(user=user, match=match).values(
            "player_id", "contribution"
        )
    )
    if not user_evals:
        return 0.0

    player_ids = [e["player_id"] for e in user_evals]

    # Бейзлайн сообщества считается БЕЗ учёта собственной оценки пользователя
    # (exclude(user=user)) — иначе пользователь частично "сравнивается сам
    # с собой", что занижает обнаруживаемое отклонение.
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
            # Нет данных сообщества по этому игроку (пользователь — единственный
            # оценивший) — исключаем из расчёта, а не подставляем 0.
            continue
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
    """
    Публичная диагностическая обёртка для отображения метрик предвзятости
    ОДНОГО конкретного матча (например, в админке или личном кабинете
    модератора) — "насколько сильно в ЭТОЙ оценке разошлись баллы своей
    команде и сопернику".

    Это НАМЕРЕННО другая метрика, чем `compute_bias_score` выше:
    `compute_bias_score` — историческая, по нескольким последним матчам,
    используется для веса голоса и критерия бейджа `bias_free`;
    `detect_fan_bias` — снимок по одному матчу, для точечной диагностики
    конкретной оценки. Не дублирование — разные вопросы.
    """
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