# users/services.py
"""
Проверка и выдача достижений. accurate_analyst/bias_free группируют оценки
по match_id одним запросом (.values().annotate()), не N+1 в цикле по
матчам; bias_free переиспользует aggregates.services.compute_bias_score —
единый источник исторического bias-score. check_and_award_badges вызывается
только асинхронно из users/tasks.py::check_and_award_badges_task
(transaction.on_commit в evaluations/views.py) — до ~100 запросов, которые
раньше блокировали HTTP-цикл на каждом завершении оценки.

founder выдаётся отдельно при верификации email (VerifyEmailView), не
здесь — разовое событие. monthly_champion — отдельная периодическая задача
(award_monthly_champion_badge), тоже не событие на оценку. derby_hunter
считает дерби-матчи в Python по Team.rivals — join по M2M здесь менее
читаем, чем цикл по небольшому ограниченному списку пар.
"""
from __future__ import annotations

import logging

from django.db.models import Avg, F
from datetime import timedelta

from aggregates.services import compute_bias_score
from evaluations.models import ContextEvaluation, CoachEvaluation, PlayerEvaluation, RefereeEvaluation
from users.models import UserBadge

logger = logging.getLogger(__name__)

ACCURATE_ANALYST_LOOKBACK = 20
ACCURATE_ANALYST_MAX_DEVIATION = 1.0
ACCURATE_ANALYST_MIN_ACCURATE_RATIO = 0.8

BIAS_FREE_LOOKBACK = 15
BIAS_FREE_MAX_SCORE = 0.15  # см. пункт 2 докстринга — доля матчей с ЭКСТРЕМАЛЬНЫМ перекосом

FORESIGHT_MIN_EVALUATIONS = 30
FORESIGHT_MIN_TRUST_SCORE = 1.6

JUDGE_OF_JUDGES_MIN_COUNT = 25
POLYGLOT_MIN_TEAMS = 8

DERBY_HUNTER_MIN_MATCHES = 5

# --- НОВОЕ (2026-09-01, продуктовый запрос "достижения по оценкам и
# прогнозам + супер-ультра уровень") — константы для 11 новых достижений
# (users/badges.py). Обоснование каждого порога — в докстринге
# соответствующей _maybe_award_* функции ниже.
COACH_EXPERT_MIN_COUNT = 25
BOTH_SIDES_MIN_MATCHES = 15
FULL_SEASON_MIN_TOURS = 10  # отсекаем куцые/недоигранные сезоны от срабатывания "задаром"
SEASON_COMPLETIONIST_MIN_MATCHES = 30  # тот же смысл, для "Стоглазого"
STABLE_HAND_MIN_PREDICTIONS = 50
STABLE_HAND_MIN_ACCURACY = 0.85
DERBY_PROPHET_MIN_CORRECT = 5
AGAINST_THE_TIDE_MIN_TOTAL_PREDICTIONS = 5  # чтобы "меньшинство" было осмысленным, не 1 из 2
PERFECT_TOUR_MIN_MATCHES = 6
MAX_TRUST_THRESHOLD = 1.95  # потолок формулы — 2.0 (см. User.trust_score/update_evaluation_stats)
MAX_TRUST_MIN_EVALUATIONS = 100


def check_and_award_badges(user) -> list[UserBadge]:
    """
    Проверяет условия и выдаёт достижения. Возвращает список только что
    созданных объектов `UserBadge` (уже существовавшие — не возвращаются).

    ВАЖНО: вызывать эту функцию нужно ТОЛЬКО из асинхронного контекста
    (Celery-задача), не из HTTP request-response цикла — см. пункт 3
    докстринга модуля.
    """
    awarded: list[UserBadge] = []
    total = user.total_evaluations
    streak = user.evaluation_streak

    try:
        if total >= 1:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="first_evaluation")
            if created:
                awarded.append(b)

        if total >= 10:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="active_fan_10")
            if created:
                awarded.append(b)

        if total >= 50:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="active_fan_50")
            if created:
                awarded.append(b)

        if total >= 150:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="active_fan_150")
            if created:
                awarded.append(b)

        if total >= ACCURATE_ANALYST_LOOKBACK:
            _maybe_award_accurate_analyst(user, awarded)

        if total >= BIAS_FREE_LOOKBACK:
            _maybe_award_bias_free(user, awarded)

        if total >= 5:
            early_count = ContextEvaluation.objects.filter(
                user=user,
                match__end_time__isnull=False,
                created_at__lte=F("match__end_time") + timedelta(hours=2),
            ).count()
            if early_count >= 5:
                b, created = UserBadge.objects.get_or_create(user=user, badge_type="early_bird")
                if created:
                    awarded.append(b)

        if streak >= 7:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_7")
            if created:
                awarded.append(b)

        if streak >= 30:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_30")
            if created:
                awarded.append(b)

        if streak >= 100:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_100")
            if created:
                awarded.append(b)

        if total >= FORESIGHT_MIN_EVALUATIONS and user.trust_score >= FORESIGHT_MIN_TRUST_SCORE:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="foresight")
            if created:
                awarded.append(b)

        if RefereeEvaluation.objects.filter(user=user).count() >= JUDGE_OF_JUDGES_MIN_COUNT:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="judge_of_judges")
            if created:
                awarded.append(b)

        distinct_teams = (
            PlayerEvaluation.objects.filter(user=user)
            .values("player__team_id")
            .distinct()
            .count()
        )
        if distinct_teams >= POLYGLOT_MIN_TEAMS:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="polyglot")
            if created:
                awarded.append(b)

        if total >= DERBY_HUNTER_MIN_MATCHES:
            _maybe_award_derby_hunter(user, awarded)

        # НОВОЕ (retention loop "Серии", 2026-08-21; семантика поля
        # ПЕРЕСМОТРЕНА 2026-08-31): прогнозы 1X2 — ПАРАЛЛЕЛЬНЫЙ набор
        # бейджей поверх user.prediction_streak (см. users/models.py::
        # User.update_prediction_stats — с 2026-08-31 это подряд УГАДАННЫЕ
        # исходы, не дни активности), тот же порог 7/30/100, что и у
        # streak_7/30/100, но осознанно НЕ переиспользует их — прогноз и
        # оценка разная активность, см. докстринг у поля.
        # Не отдельный денормализованный счётчик на User (в отличие от
        # total_evaluations) — прогнозов на порядки меньше оценок за один
        # матч (одна запись MatchPrediction на пару user+match), COUNT()
        # здесь дешёвый и не требует поддерживать ещё одно поле в синхроне.
        if user.match_predictions.exists():
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="first_prediction")
            if created:
                awarded.append(b)

        prediction_streak = user.prediction_streak
        if prediction_streak >= 7:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_7")
            if created:
                awarded.append(b)
        if prediction_streak >= 30:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_30")
            if created:
                awarded.append(b)
        if prediction_streak >= 100:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_100")
            if created:
                awarded.append(b)

        # --- НОВОЕ (2026-09-01) — 11 достижений поверх существовавших 20,
        # включая 5 legendary ("супер ультра"). Пороги/названия — см.
        # users/badges.py, обоснование каждого условия — в докстринге
        # соответствующей _maybe_award_* функции ниже.
        if streak >= 250:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="streak_250")
            if created:
                awarded.append(b)

        if prediction_streak >= 200:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="prediction_streak_200")
            if created:
                awarded.append(b)

        if CoachEvaluation.objects.filter(user=user).count() >= COACH_EXPERT_MIN_COUNT:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="coach_expert")
            if created:
                awarded.append(b)

        if user.trust_score >= MAX_TRUST_THRESHOLD and total >= MAX_TRUST_MIN_EVALUATIONS:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="max_trust")
            if created:
                awarded.append(b)

        if total >= BOTH_SIDES_MIN_MATCHES:
            _maybe_award_both_sides(user, awarded)

        if total >= FULL_SEASON_MIN_TOURS:
            _maybe_award_full_season(user, awarded)

        if total >= SEASON_COMPLETIONIST_MIN_MATCHES:
            _maybe_award_season_completionist(user, awarded)

        if user.match_predictions.filter(match__status="finished").count() >= STABLE_HAND_MIN_PREDICTIONS:
            _maybe_award_stable_hand(user, awarded)

        if user.match_predictions.filter(match__status="finished").count() >= DERBY_PROPHET_MIN_CORRECT:
            _maybe_award_derby_prophet(user, awarded)

        if user.match_predictions.filter(match__status="finished").count() >= AGAINST_THE_TIDE_MIN_TOTAL_PREDICTIONS:
            _maybe_award_against_the_tide(user, awarded)

        if user.match_predictions.filter(match__status="finished").exists():
            _maybe_award_perfect_tour(user, awarded)

    except Exception as e:
        logger.error("Ошибка проверки достижений для %s: %s", user.username, e, exc_info=True)

    return awarded


def _maybe_award_both_sides(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Обе стороны»: в ≥`BOTH_SIDES_MIN_MATCHES` матчах пользователь
    оценил игроков ОБЕИХ команд, а не только своих фанатов/одной стороны.

    Команда игрока НА КОНКРЕТНЫЙ МАТЧ берётся через `MatchLineupPlayer.
    lineup.team` (та же логика, что и career_by_season в players/views.py,
    и фикс "топ игроков команды" в teams/views.py от 2026-09-01) — НЕ через
    текущий `Player.team`, который может не совпадать с командой на момент
    того конкретного матча при трансфере игрока в межсезонье.

    2 запроса всего (не N+1 по матчам): один batch на все пары (player,
    match) пользователя, один batch на связку с MatchLineupPlayer.
    """
    from lineups.models import MatchLineupPlayer

    pairs = list(
        PlayerEvaluation.objects.filter(user=user)
        .values("match_id", "player_id")
        .distinct()
    )
    if not pairs:
        return

    match_ids = {r["match_id"] for r in pairs}
    player_ids = {r["player_id"] for r in pairs}
    lineup_map = {
        (mlp.player_id, mlp.lineup.match_id): mlp.lineup.team_id
        for mlp in MatchLineupPlayer.objects.filter(
            player_id__in=player_ids, lineup__match_id__in=match_ids
        ).select_related("lineup")
    }

    teams_by_match: dict[str, set] = {}
    for r in pairs:
        team_id = lineup_map.get((r["player_id"], r["match_id"]))
        if team_id is None:
            continue
        teams_by_match.setdefault(r["match_id"], set()).add(team_id)

    both_sides_count = sum(1 for teams in teams_by_match.values() if len(teams) >= 2)
    if both_sides_count >= BOTH_SIDES_MIN_MATCHES:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="both_sides")
        if created:
            awarded.append(b)


def _maybe_award_full_season(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Полный сезон»: хотя бы один оценённый матч в КАЖДОМ туре, где в
    сезоне вообще были матчи (без пропусков — необязательно подряд по
    датам, в отличие от streak_*, который рвётся при любом пропущенном
    туре). Источник "оценил матч" — `EvaluationSession.status='completed'`,
    тот же, что и `stats.total_matches` в профиле (users/views.py), а не
    факт наличия любых evaluation-строк.

    `FULL_SEASON_MIN_TOURS` отсекает куцые/ещё не доигранные сезоны — иначе
    в первом же туре нового сезона любой активный пользователь получил бы
    "полный сезон" из одного матча.
    """
    from matches.models import Match

    season_ids = list(
        user.evaluation_sessions.filter(status="completed", match__season__isnull=False)
        .values_list("match__season_id", flat=True)
        .distinct()
    )
    for season_id in season_ids:
        all_tours = set(
            Match.objects.filter(season_id=season_id, tour__isnull=False)
            .values_list("tour", flat=True)
            .distinct()
        )
        if len(all_tours) < FULL_SEASON_MIN_TOURS:
            continue
        evaluated_tours = set(
            user.evaluation_sessions.filter(
                status="completed", match__season_id=season_id, match__tour__isnull=False
            )
            .values_list("match__tour", flat=True)
            .distinct()
        )
        if all_tours <= evaluated_tours:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="full_season")
            if created:
                awarded.append(b)
            return


def _maybe_award_season_completionist(user, awarded: list[UserBadge]) -> None:
    """
    Legendary-бейдж «Стоглазый»: оценены ВСЕ без исключения завершённые
    матчи одного полного сезона — не по одному на тур (это «Полный сезон»
    выше), а буквально каждый сыгранный матч. Знаменатель —
    `Match.status='finished'` в сезоне (перенесённые/отменённые не в счёт,
    их физически нельзя оценить); `SEASON_COMPLETIONIST_MIN_MATCHES`
    защищает от срабатывания на сезоне, где сыграно всего пара туров.
    """
    from matches.models import Match

    season_ids = list(
        user.evaluation_sessions.filter(status="completed", match__season__isnull=False)
        .values_list("match__season_id", flat=True)
        .distinct()
    )
    for season_id in season_ids:
        total_matches = Match.objects.filter(season_id=season_id, status="finished").count()
        if total_matches < SEASON_COMPLETIONIST_MIN_MATCHES:
            continue
        evaluated_matches = (
            user.evaluation_sessions.filter(
                status="completed", match__season_id=season_id, match__status="finished"
            )
            .values("match_id")
            .distinct()
            .count()
        )
        if evaluated_matches >= total_matches:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="season_completionist")
            if created:
                awarded.append(b)
            return


def _maybe_award_stable_hand(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Стабильная рука»: ≥`STABLE_HAND_MIN_PREDICTIONS` прогнозов на
    завершённые матчи с точностью ≥`STABLE_HAND_MIN_ACCURACY`. В отличие от
    prediction_streak_* (рвётся при ЛЮБОЙ ошибке), это метрика качества НА
    ОБЪЁМЕ — позволяет редкие промахи, если общая точность всё равно высокая.

    `MatchPrediction.is_correct` — property, не поле БД (сверяет `choice` с
    `match.final_result`, тоже property) — посчитать через `.filter()`
    нельзя, поэтому один проход по `select_related('match')`, без N+1.
    """
    predictions = user.match_predictions.filter(match__status="finished").select_related("match")
    total = 0
    correct = 0
    for p in predictions:
        is_correct = p.is_correct
        if is_correct is None:
            continue
        total += 1
        if is_correct:
            correct += 1
    if total >= STABLE_HAND_MIN_PREDICTIONS and correct / total >= STABLE_HAND_MIN_ACCURACY:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="stable_hand")
        if created:
            awarded.append(b)


def _maybe_award_derby_prophet(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Дерби-пророк»: ≥`DERBY_PROPHET_MIN_CORRECT` угаданных прогнозов
    на матчи между принципиальными соперниками (`Team.rivals`) — прогнозный
    аналог `derby_hunter` (тот про оценки, этот про прогнозы).
    """
    from teams.models import Team

    rival_pairs: set[frozenset] = {
        frozenset((from_id, to_id))
        for from_id, to_id in Team.rivals.through.objects.values_list("from_team_id", "to_team_id")
    }
    if not rival_pairs:
        return

    predictions = user.match_predictions.filter(match__status="finished").select_related("match")
    correct_derby_count = sum(
        1
        for p in predictions
        if frozenset((p.match.home_team_id, p.match.away_team_id)) in rival_pairs and p.is_correct
    )
    if correct_derby_count >= DERBY_PROPHET_MIN_CORRECT:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="derby_prophet")
        if created:
            awarded.append(b)


def _maybe_award_against_the_tide(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Против течения»: хотя бы ОДИН раз пользователь угадал исход,
    когда его выбор был в МЕНЬШИНСТВЕ голосов сообщества по этому матчу
    (contrarian call, который сбылся). "Меньшинство" — выбор пользователя
    НЕ совпадает с вариантом, набравшим больше всего голосов среди ВСЕХ
    прогнозов на этот матч. `AGAINST_THE_TIDE_MIN_TOTAL_PREDICTIONS`
    защищает от тривиального случая "нас было двое, я не как он".
    """
    from django.db.models import Count

    from predictions.models import MatchPrediction

    user_correct = [
        p
        for p in user.match_predictions.filter(match__status="finished").select_related("match")
        if p.is_correct
    ]
    if not user_correct:
        return

    match_ids = [p.match_id for p in user_correct]
    counts_by_match: dict[str, dict[str, int]] = {}
    for row in (
        MatchPrediction.objects.filter(match_id__in=match_ids)
        .values("match_id", "choice")
        .annotate(c=Count("id"))
    ):
        counts_by_match.setdefault(row["match_id"], {})[row["choice"]] = row["c"]

    for p in user_correct:
        counts = counts_by_match.get(p.match_id, {})
        total_votes = sum(counts.values())
        if total_votes < AGAINST_THE_TIDE_MIN_TOTAL_PREDICTIONS:
            continue
        majority_choice = max(counts, key=counts.get)
        if p.choice != majority_choice:
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="against_the_tide")
            if created:
                awarded.append(b)
            return


def _maybe_award_perfect_tour(user, awarded: list[UserBadge]) -> None:
    """
    Legendary-бейдж «Идеальный тур»: пользователь спрогнозировал АБСОЛЮТНО
    ВСЕ матчи одного тура (не часть — именно все, при размере тура
    ≥`PERFECT_TOUR_MIN_MATCHES`) и угадал исход КАЖДОГО. Перебираем только
    (сезон, тур) пары, где пользователь вообще что-то прогнозировал — не
    гоняем по всем турам лиги без разбора.
    """
    from matches.models import Match

    tour_season_pairs = (
        user.match_predictions.filter(match__tour__isnull=False)
        .values_list("match__season_id", "match__tour")
        .distinct()
    )
    for season_id, tour in tour_season_pairs:
        tour_matches = list(Match.objects.filter(season_id=season_id, tour=tour, status="finished"))
        if len(tour_matches) < PERFECT_TOUR_MIN_MATCHES:
            continue
        tour_match_ids = {m.id for m in tour_matches}
        user_predictions = {
            p.match_id: p
            for p in user.match_predictions.filter(match_id__in=tour_match_ids).select_related("match")
        }
        if set(user_predictions.keys()) != tour_match_ids:
            continue  # спрогнозировал не ВСЕ матчи тура
        if all(p.is_correct for p in user_predictions.values()):
            b, created = UserBadge.objects.get_or_create(user=user, badge_type="perfect_tour")
            if created:
                awarded.append(b)
            return


def _maybe_award_accurate_analyst(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Точный аналитик»: отклонение ≤1.0 от среднего сообщества
    (БЕЗ учёта собственной оценки пользователя) в ≥80% из последних 20
    матчей, где пользователь указывал контекст просмотра.

    2 запроса ВСЕГО вместо до 40 (см. пункт 1 докстринга модуля).
    """
    recent_match_ids = list(
        ContextEvaluation.objects.filter(user=user, match__isnull=False)
        .order_by("-created_at")
        .values_list("match_id", flat=True)[:ACCURATE_ANALYST_LOOKBACK]
    )
    if not recent_match_ids:
        return

    user_avg_by_match = dict(
        PlayerEvaluation.objects.filter(user=user, match_id__in=recent_match_ids)
        .values("match_id")
        .annotate(avg=Avg("contribution"))
        .values_list("match_id", "avg")
    )
    community_avg_by_match = dict(
        PlayerEvaluation.objects.filter(match_id__in=recent_match_ids)
        .exclude(user=user)
        .values("match_id")
        .annotate(avg=Avg("contribution"))
        .values_list("match_id", "avg")
    )

    accurate = sum(
        1
        for match_id in recent_match_ids
        if match_id in user_avg_by_match
        and match_id in community_avg_by_match
        and abs(user_avg_by_match[match_id] - community_avg_by_match[match_id]) <= ACCURATE_ANALYST_MAX_DEVIATION
    )

    if accurate >= len(recent_match_ids) * ACCURATE_ANALYST_MIN_ACCURATE_RATIO:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="accurate_analyst")
        if created:
            awarded.append(b)


def _maybe_award_bias_free(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Без предвзятости»: `compute_bias_score` (единая реализация из
    `aggregates.services`, см. пункт 2 докстринга модуля) по последним
    матчам поддерживаемой команды ниже `BIAS_FREE_MAX_SCORE`.
    """
    latest_context = (
        ContextEvaluation.objects.filter(user=user, supported_team__isnull=False, match__isnull=False)
        .select_related("match")
        .order_by("-created_at")
        .first()
    )
    if not latest_context or not latest_context.match_id:
        return

    bias_score = compute_bias_score(user, latest_context.match, lookback=BIAS_FREE_LOOKBACK)
    if bias_score <= BIAS_FREE_MAX_SCORE:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="bias_free")
        if created:
            awarded.append(b)


def _maybe_award_derby_hunter(user, awarded: list[UserBadge]) -> None:
    """
    Бейдж «Дерби-эксперт»: оценено ≥`DERBY_HUNTER_MIN_MATCHES` матчей между
    командами, отмеченными друг у друга как соперники (`Team.rivals`,
    проставляется вручную в админке — см. `teams/admin.py`). Список
    соперничеств — продуктовое решение, не автоматика.

    Список пар соперников в лиге заведомо маленький (десятки, не тысячи) —
    поэтому сравнение "матч — это дерби?" делается один раз в Python по
    множеству пар, а не через `.filter()` с M2M-джойном на каждый матч.
    """
    from matches.models import Match
    from teams.models import Team

    rival_pairs: set[frozenset] = {
        frozenset((from_id, to_id))
        for from_id, to_id in Team.rivals.through.objects.values_list("from_team_id", "to_team_id")
    }
    if not rival_pairs:
        return

    evaluated_matches = (
        Match.objects.filter(context_evaluations__user=user)
        .values_list("id", "home_team_id", "away_team_id")
        .distinct()
    )

    derby_count = sum(
        1
        for _match_id, home_id, away_id in evaluated_matches
        if frozenset((home_id, away_id)) in rival_pairs
    )

    if derby_count >= DERBY_HUNTER_MIN_MATCHES:
        b, created = UserBadge.objects.get_or_create(user=user, badge_type="derby_hunter")
        if created:
            awarded.append(b)