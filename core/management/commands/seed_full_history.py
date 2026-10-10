# core/management/commands/seed_full_history.py
"""manage.py seed_full_history [--season-id UUID] [--tour-from N] [--tour-to N]
                             [--pool-size N] [--seed N] [--no-recalc] [--no-badges]

Наполняет историю сезона тестовыми данными ботов, тур за туром:
  1. оценки + EvaluationSession(completed) + update_evaluation_stats;
  2. XP одним вызовом add_xp на матч;
  3. прогнозы на завершённые матчи + update_prediction_stats;
  4. синхронный пересчёт агрегатов матча;
  5. сборная тура; 6. сборная сезона и таблица;
  7. check_and_award_badges напрямую (без уведомлений);
  8. live-реакции 👍/👎 на ключевые события — по тому, за кого болеет бот; перелом матча — конкретное событие.
Работает в тихом режиме (core.bulk): сигналы не ставят задачи в Celery, итоги туров не рассылаются.

Пул ботов общий с seed_match_votes (test_user_bot_NNNN@test.dopx.local).
Трейты бота (вовлечённость, меткость) детерминированы от username.
trust_score не пересчитывается по формуле — только начальный разброс.

Очистка: python manage.py cleanup_test_users --apply
"""
from __future__ import annotations

import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from evaluations.models import (
    ContextEvaluation,
    CoachEvaluation,
    EvaluationSession,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from events.models import EventReaction
from lineups.models import MatchLineupPlayer
from matches.models import Match
from players.models import Player
from predictions.models import MatchPrediction
from seasons.models import Season
from users.models import UserXP
from core.management.seed_guard import ensure_seed_allowed

User = get_user_model()

BOT_USERNAME_PREFIX = "test_user_bot_"
BOT_EMAIL_DOMAIN = "test.dopx.local"
WATCHED_TYPES = ["full", "full", "full", "highlights", "partial"]

# Сумма XP за все шаги вайзарда (2+2+3+1+1+1).
EVALUATION_XP_PER_MATCH = 10

# Средняя меткость прогнозов 1X2 и её разброс.
PREDICTION_BASE_ACCURACY = 0.47
PREDICTION_ACCURACY_STDDEV = 0.10
PREDICTION_ACCURACY_MIN = 0.28
PREDICTION_ACCURACY_MAX = 0.72

# При промахе ничья выбирается реже.
WRONG_CHOICE_WEIGHTS = {"1": 1.0, "X": 0.5, "2": 1.0}

# Live-реакции: на какие события и с какой вероятностью бот вообще жмёт кнопку.
REACTION_EVENT_TYPES = ("goal", "own_goal", "penalty", "red_card", "disallowed_goal", "var_check", "yellow_card")
REACTION_SHARE = {"goal": 0.75, "penalty": 0.7, "own_goal": 0.6, "red_card": 0.6, "disallowed_goal": 0.6,
                  "var_check": 0.45, "yellow_card": 0.2}
# Перелом матча называют среди этих событий.
TURNING_EVENT_TYPES = ("goal", "own_goal", "penalty", "red_card", "disallowed_goal", "var_check")

class Command(BaseCommand):
    help = (
        "Наполняет ВСЮ историю сезона (оценки + прогнозы + серии + XP + "
        "бейджи + сборные тура/сезона + турнирная таблица) синтетическими "
        "данными переиспользуемого пула ботов — качественная имитация для "
        "полноценной проверки функционала, не единичные тестовые матчи."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--season-id", type=str, default=None,
            help="UUID сезона (по умолчанию — Season.get_primary_active()).",
        )
        parser.add_argument(
            "--tour-from", type=int, default=None,
            help="Не трогать туры МЕНЬШЕ этого номера (по умолчанию — с первого сыгранного).",
        )
        parser.add_argument(
            "--tour-to", type=int, default=None,
            help="Не трогать туры БОЛЬШЕ этого номера (по умолчанию — по последний сыгранный).",
        )
        parser.add_argument(
            "--pool-size", type=int, default=150,
            help="Размер переиспользуемого пула ботов (общий с seed_match_votes, по умолчанию 150).",
        )
        parser.add_argument(
            "--seed", type=int, default=None,
            help="Seed для random — одинаковый набор участников/прогнозов при повторном запуске.",
        )
        parser.add_argument(
            "--no-recalc", action="store_true",
            help="Не пересчитывать агрегаты/сборные/таблицу (только создать сырые оценки и прогнозы).",
        )
        parser.add_argument(
            "--no-badges", action="store_true",
            help="Не запускать финальную проверку достижений (быстрее, но бейджи не появятся).",
        )

    def handle(self, *args, **options):
        ensure_seed_allowed()
        from core.bulk import quiet

        # Скрипт пересчитывает агрегаты, туры и бейджи сам — фоновые задачи от сигналов не нужны.
        with quiet():
            self._run(options)

    def _run(self, options):
        if options["seed"] is not None:
            random.seed(options["seed"])

        season = self._resolve_season(options["season_id"])
        self.stdout.write(f"Сезон: {season} (лига {season.league}).")

        matches = self._resolve_matches(season, options["tour_from"], options["tour_to"])
        if not matches:
            raise CommandError("Не найдено ни одного завершённого матча с проставленным туром в этом сезоне.")

        tours = sorted({m.tour for m in matches})
        self.stdout.write(
            f"Найдено {len(matches)} завершённых матчей, туры {tours[0]}–{tours[-1]} ({len(tours)} туров)."
        )

        pool = self._ensure_bot_pool(options["pool_size"])
        self.stdout.write(f"Пул ботов готов: {len(pool)} (префикс {BOT_USERNAME_PREFIX}*, общий с seed_match_votes).")

        touched_user_ids: set[str] = set()
        total_evaluation_voters = 0
        total_predictions = 0

        matches_by_tour: dict[int, list[Match]] = {}
        for m in matches:
            matches_by_tour.setdefault(m.tour, []).append(m)

        for tour in tours:
            tour_matches = matches_by_tour[tour]
            self.stdout.write(f"Тур {tour}: {len(tour_matches)} матч(ей)...")
            for match in tour_matches:
                n_voters, n_predictions, touched = self._seed_match(match, pool)
                total_evaluation_voters += n_voters
                total_predictions += n_predictions
                touched_user_ids |= touched
                self.stdout.write(
                    f"    {match.home_team.name} vs {match.away_team.name}: "
                    f"{n_voters} оценок, {n_predictions} прогнозов."
                )
                if not options["no_recalc"]:
                    self._recalculate_match_synchronously(str(match.id))

            if not options["no_recalc"]:
                self._recompute_round(season, tour)

        if not options["no_recalc"]:
            self._recompute_season_squad_and_standings(season)

        self.stdout.write(self.style.SUCCESS(
            f"Готово: {len(matches)} матчей, {total_evaluation_voters} голосований, "
            f"{total_predictions} прогнозов, {len(touched_user_ids)} затронутых ботов."
        ))

        if not options["no_badges"]:
            awarded_total = self._award_badges(touched_user_ids)
            self.stdout.write(self.style.SUCCESS(f"Достижений выдано: {awarded_total}."))
        else:
            self.stdout.write(self.style.WARNING("--no-badges: достижения не проверялись."))

        self.stdout.write(self.style.SUCCESS(
            "Сброс — python manage.py cleanup_test_users --apply "
            "(удаляет ботов и каскадно всё, что на них завязано)."
        ))

    # ------------------------------------------------------------------

    def _resolve_season(self, season_id: str | None) -> Season:
        if season_id:
            try:
                return Season.objects.select_related("league").get(id=season_id)
            except Season.DoesNotExist as exc:
                raise CommandError(f"Сезон {season_id!r} не найден.") from exc
        season = Season.get_primary_active()
        if season is None:
            raise CommandError("Активный сезон не найден — укажите --season-id явно.")
        return season

    def _resolve_matches(self, season: Season, tour_from: int | None, tour_to: int | None) -> list[Match]:
        qs = (
            Match.objects.filter(season=season, status="finished", tour__isnull=False)
            .select_related("home_team", "away_team", "referee", "home_coach", "away_coach")
            .order_by("tour", "start_time")
        )
        if tour_from is not None:
            qs = qs.filter(tour__gte=tour_from)
        if tour_to is not None:
            qs = qs.filter(tour__lte=tour_to)
        return list(qs)

    def _ensure_bot_pool(self, pool_size: int) -> list:
        """Тот же пул, что в seed_match_votes._ensure_bot_pool."""
        existing = list(User.objects.filter(username__startswith=BOT_USERNAME_PREFIX).order_by("username"))
        if len(existing) >= pool_size:
            return existing[:pool_size]

        created = list(existing)
        for i in range(len(existing) + 1, pool_size + 1):
            username = f"{BOT_USERNAME_PREFIX}{i:04d}"
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@{BOT_EMAIL_DOMAIN}",
                    "is_verified": True,
                    "trust_score": round(random.uniform(0.7, 1.5), 2),
                },
            )
            if was_created:
                user.set_password("not-a-real-account")
                user.save(update_fields=["password"])
                UserXP.objects.get_or_create(user=user)
            created.append(user)
        return created

    def _bot_traits(self, user) -> tuple[float, float]:
        """Детерминированные трейты бота через отдельный random.Random(username)."""
        r = random.Random(user.username)
        engagement = min(0.95, r.random() ** 1.7)
        accuracy = max(
            PREDICTION_ACCURACY_MIN,
            min(PREDICTION_ACCURACY_MAX, r.gauss(PREDICTION_BASE_ACCURACY, PREDICTION_ACCURACY_STDDEV)),
        )
        return engagement, accuracy

    def _seed_match(self, match: Match, pool: list) -> tuple[int, int, set[str]]:
        touched: set[str] = set()
        n_voters = self._seed_evaluations(match, pool, touched)
        n_predictions = self._seed_predictions(match, pool, touched)
        self._seed_reactions(match, pool)
        return n_voters, n_predictions, touched

    def _seed_evaluations(self, match: Match, pool: list, touched: set[str]) -> int:
        """Голоса ботов за матч: строки собираются в памяти и пишутся пачками (bulk_create),
        а не по одной — иначе тысячи запросов на матч."""
        lineup_players = list(
            Player.objects.filter(matchlineupplayer__in=MatchLineupPlayer.objects.filter(lineup__match=match).played())
            .distinct()
        )
        player_quality = {p.id: random.uniform(3.0, 9.0) for p in lineup_players}
        coverage = random.uniform(0.5, 0.85)  # разный охват игроков от матча к матчу
        # Поздние события чаще называют переломом.
        turning_events = list(match.events.filter(event_type__in=TURNING_EVENT_TYPES).order_by("minute"))
        turning_weights = [1 + e.minute / 30 for e in turning_events]
        # Сессия уже есть — матч для бота обработан, пропускаем (иначе счётчики удвоятся).
        done = set(EvaluationSession.objects.filter(match=match, user__in=pool).values_list("user_id", flat=True))
        now = timezone.now()

        rows: dict[type, list] = {model: [] for model in (
            EvaluationSession, ContextEvaluation, MatchEvaluation, RefereeEvaluation,
            TeamEvaluation, CoachEvaluation, PlayerEvaluation)}
        voters = []
        for voter in pool:
            engagement, _accuracy = self._bot_traits(voter)
            if random.random() >= engagement:
                continue  # бот пропустил матч
            if voter.id in done:
                touched.add(str(voter.id))
                continue
            voters.append(voter)
            rows[EvaluationSession].append(EvaluationSession(
                user=voter, match=match, mode=random.choice(["quick", "quick", "full"]), status="completed",
                completed_steps=["context", "teams", "players", "coaches", "referee", "match_eval"],
                # Внутри окна голосования (иначе audit_data видит «оценку вне окна»): 2–40 ч после старта.
                current_step="complete",
                completed_at=min(now, match.start_time + timedelta(hours=random.uniform(2, 40))),
            ))
            rows[ContextEvaluation].append(ContextEvaluation(
                user=voter, match=match, watched_type=random.choice(WATCHED_TYPES),
                attended_stadium=random.random() < 0.15,
                supported_team=random.choices([match.home_team, match.away_team, None], weights=[0.4, 0.4, 0.2])[0],
            ))
            turning = random.random() < 0.35
            turning_event = (random.choices(turning_events, weights=turning_weights)[0]
                             if turning and turning_events and random.random() < 0.8 else None)
            rows[MatchEvaluation].append(MatchEvaluation(
                user=voter, match=match,
                entertainment=_clamp(round(random.gauss(6.5, 2.0)), 1, 10),
                tension=_clamp(round(random.gauss(6.0, 2.0)), 1, 10),
                fairness=_clamp(round(random.gauss(6.5, 2.0)), 1, 10),
                turning_point=turning, turning_point_event=turning_event,
                turning_point_kind="" if turning_event or not turning else random.choice(
                    ["referee_decision", "substitution", "tactics", "save", "missed_chance", "momentum"]),
            ))
            if match.referee_id:
                rows[RefereeEvaluation].append(RefereeEvaluation(
                    user=voter, match=match,
                    influence_score=_clamp(round(random.gauss(35, 20)), 0, 100),
                    decision_quality=_clamp(round(random.gauss(6.5, 2.0)), 1, 10),
                ))
            for team in (match.home_team, match.away_team):
                rows[TeamEvaluation].append(TeamEvaluation(
                    user=voter, match=match, team=team,
                    tactics=_clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                    effort=_clamp(round(random.gauss(6.8, 1.8)), 1, 10),
                    organization=_clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                    mentality=_clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                ))
            for coach in (match.home_coach, match.away_coach):
                if coach is not None:
                    rows[CoachEvaluation].append(CoachEvaluation(
                        user=voter, match=match, coach=coach,
                        tactics=_clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                        substitutions=_clamp(round(random.gauss(6.0, 1.8)), 1, 10),
                        game_management=_clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                        impact=_clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                    ))
            for player in lineup_players:
                if random.random() > coverage:
                    continue
                quality = player_quality[player.id]
                rows[PlayerEvaluation].append(PlayerEvaluation(
                    user=voter, match=match, player=player,
                    contribution=_clamp(round(random.gauss(quality, 1.5)), 1, 10),
                    risk=_clamp(round(random.gauss(10 - quality, 1.5)), 1, 10),
                    potential=_clamp(round(random.gauss(quality, 1.5)), 1, 10),
                ))

        if not voters:
            return 0
        with transaction.atomic():
            # ignore_conflicts — остатки прерванного прогона (оценка без сессии) не роняют повтор.
            for model, objs in rows.items():
                model.objects.bulk_create(objs, ignore_conflicts=True, batch_size=1000)
            for voter in voters:
                voter.apply_evaluation_to_streak(match)
                voter.updated_at = now
            User.objects.bulk_update(voters, ["total_evaluations", "evaluation_streak", "last_evaluation_season_id",
                                              "last_evaluation_tour", "updated_at"], batch_size=500)
            xp_by_user = {xp.user_id: xp for xp in UserXP.objects.filter(user__in=voters)}
            for voter in voters:
                xp = xp_by_user.get(voter.id) or UserXP.objects.create(user=voter)
                xp.add_xp(round(EVALUATION_XP_PER_MATCH * voter.xp_multiplier()))
                touched.add(str(voter.id))
        return len(voters)

    def _seed_predictions(self, match: Match, pool: list, touched: set[str]) -> int:
        final_result = match.final_result
        if final_result is None:
            return 0  # прогнозы только на матчи с известным исходом

        # Уже есть прогноз — не трогаем, иначе серия удвоится.
        done = set(MatchPrediction.objects.filter(match=match, user__in=pool).values_list("user_id", flat=True))
        predictions, predictors = [], []
        for predictor in pool:
            engagement, accuracy = self._bot_traits(predictor)
            if random.random() >= engagement:
                continue
            if predictor.id in done:
                touched.add(str(predictor.id))
                continue
            if random.random() < accuracy:
                choice = final_result
            else:
                others = [c for c in ("1", "X", "2") if c != final_result]
                choice = random.choices(others, weights=[WRONG_CHOICE_WEIGHTS[c] for c in others])[0]
            predictions.append(MatchPrediction(user=predictor, match=match, choice=choice))
            # Как User.update_prediction_stats, но без сохранения по одному.
            predictor.prediction_streak = predictor.prediction_streak + 1 if choice == final_result else 0
            predictor.updated_at = timezone.now()
            predictors.append(predictor)
            touched.add(str(predictor.id))

        # Матчи идут по порядку, поэтому серия по ним складывается правильно.
        with transaction.atomic():
            MatchPrediction.objects.bulk_create(predictions, ignore_conflicts=True, batch_size=1000)
            User.objects.bulk_update(predictors, ["prediction_streak", "updated_at"], batch_size=500)
        return len(predictions)

    def _seed_reactions(self, match: Match, pool: list) -> int:
        """👍/👎 на ключевые события: свой гол — лайк, чужой — чаще дизлайк, нейтральные — в основном лайк.
        bulk_create без сигналов (квесты ботам не нужны); повторный запуск не дублирует — уникальный ключ."""
        events = list(match.events.filter(event_type__in=REACTION_EVENT_TYPES))
        if not events:
            return 0
        sides = {}
        for user_id, team_id in ContextEvaluation.objects.filter(match=match, user__in=pool).values_list(
                "user_id", "supported_team_id"):
            sides[user_id] = ("home" if team_id == match.home_team_id else "away" if team_id == match.away_team_id
                              else None)

        rows = []
        for voter in pool:
            engagement, _accuracy = self._bot_traits(voter)
            if voter.id not in sides and random.random() >= 0.3 + engagement * 0.5:
                continue  # не оценивал матч и не смотрел live
            side = sides.get(voter.id)
            for event in events:
                if random.random() >= REACTION_SHARE[event.event_type] * (0.8 + engagement * 0.4):
                    continue
                rows.append(EventReaction(match_event=event, user=voter,
                                          reaction="like" if random.random() < _like_chance(event, side) else "dislike"))
        EventReaction.objects.bulk_create(rows, ignore_conflicts=True, batch_size=1000)
        return len(rows)

    def _recalculate_match_synchronously(self, match_id: str) -> None:
        from aggregates.tasks import (
            recalculate_coach_aggregates,
            recalculate_match_aggregate,
            recalculate_player_aggregates,
            recalculate_referee_aggregates,
            recalculate_team_aggregates,
        )

        recalculate_player_aggregates(match_id)
        recalculate_coach_aggregates(match_id)
        recalculate_team_aggregates(match_id)
        recalculate_referee_aggregates(match_id)
        try:
            recalculate_match_aggregate(match_id)
        except Exception as exc:  # noqa: BLE001 — та же причина, что в seed_match_votes
            self.stdout.write(self.style.WARNING(
                f"    Пересчёт MatchAggregate для {match_id}: {exc} (не критично)."
            ))

    def _recompute_round(self, season: Season, tour: int) -> None:
        from round_squad.services import recompute_round

        try:
            recompute_round(season, tour, force=True)  # тур мог зафиксироваться пустым до заливки голосов
        except Exception as exc:  # noqa: BLE001 — не прерываем весь бэкфилл из-за одного тура
            self.stdout.write(self.style.WARNING(f"  Пересчёт сборной тура {tour}: {exc}"))

    def _recompute_season_squad_and_standings(self, season: Season) -> None:
        from aggregates.tasks import recalculate_season_standings
        from season_squad.services import recompute_best_xi

        try:
            recompute_best_xi(season)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"Пересчёт сборной сезона: {exc}"))
        try:
            recalculate_season_standings(season_id=str(season.id))
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"Пересчёт турнирной таблицы: {exc}"))

    def _award_badges(self, user_ids: set[str]) -> int:
        from users.services import check_and_award_badges

        awarded_total = 0
        for user in User.objects.filter(id__in=user_ids):
            awarded = check_and_award_badges(user)
            awarded_total += len(awarded)
        return awarded_total


def _like_chance(event, side: str | None) -> float:
    """Вероятность 👍: событие «за» свою команду радует, «против» — нет; карточки и отмены — наоборот."""
    good_for_side = event.team_side  # чья команда выиграла от гола
    if event.event_type in ("red_card", "yellow_card", "disallowed_goal"):
        good_for_side = "away" if event.team_side == "home" else "home"  # штраф или отмена бьют по своей стороне
    if side is None:
        return 0.55 if event.event_type in ("red_card", "yellow_card", "var_check") else 0.75
    return 0.9 if side == good_for_side else 0.2


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
