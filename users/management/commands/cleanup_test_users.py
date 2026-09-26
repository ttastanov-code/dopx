# users/management/commands/cleanup_test_users.py
"""manage.py cleanup_test_users [--apply] [--no-recalc] [--reset-corrections] [--limit-preview N]

Удаляет синтетические аккаунты (боты, нагрузочные, тестовые — core.utils.synthetic_users_q)
со всеми их данными: оценки, сессии, прогнозы, реакции, XP, бейджи, подписки, push,
уведомления, флаги антифрода, события аналитики. staff/superuser не трогает.

Затем пересчитывает всё, что строилось на их голосах: агрегаты затронутых матчей,
сборные сезонов (и итоговые) и туры; турнирные таблицы не меняются.

  --no-recalc          только удалить (пересчёт — потом вручную)
  --recalc-all         пересчитать все матчи с рейтингами или оценками (если пересчёт прервался)
  --reset-corrections  обнулить авто-поправки и снять необработанные сигналы по сущностям
                       (их детекторы считали и по голосам ботов)

Без --apply — dry-run: список и счётчики, ничего не удаляется.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Count

from core.utils import synthetic_users_q
from evaluations.models import (
    CoachEvaluation,
    ContextEvaluation,
    EvaluationSession,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from predictions.models import MatchPrediction

User = get_user_model()

# Таблицы-листья без зависимых строк: их можно удалять прямым DELETE.
BULK_MODELS = (
    PlayerEvaluation, TeamEvaluation, CoachEvaluation, RefereeEvaluation,
    MatchEvaluation, ContextEvaluation, EvaluationSession, MatchPrediction,
)
# Аккаунтов в одной транзакции удаления.
DELETE_BATCH_USERS = 10


class Command(BaseCommand):
    help = "Удаляет ботов/тестовые аккаунты со всеми данными и пересчитывает рейтинги"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить")
        parser.add_argument("--no-recalc", action="store_true", help="Не пересчитывать рейтинги и сборные")
        parser.add_argument(
            "--recalc-all", action="store_true",
            help="Пересчитать все матчи с рейтингами или оценками, затем сборные и туры",
        )
        parser.add_argument(
            "--reset-corrections", action="store_true",
            help="Обнулить авто-поправки рейтингов игроков и команд (и снять их с прошлых матчей)",
        )
        parser.add_argument(
            "--limit-preview", type=int, default=50,
            help="Сколько строк списка печатать (0 — все)",
        )

    def handle(self, *args, **options):
        # Служебная команда на сотни тысяч строк: снимаем statement_timeout только для этого соединения.
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")

        user_ids = list(User.objects.filter(synthetic_users_q()).order_by("date_joined").values_list("id", flat=True))
        if not user_ids:
            self.stdout.write(self.style.SUCCESS("Синтетических аккаунтов не найдено."))
            if options["reset_corrections"]:
                self._reset_corrections()
            if options["recalc_all"]:
                self._recalculate(self._all_rated_match_ids())
            return

        limit = options["limit_preview"]
        preview_ids = user_ids if limit == 0 else user_ids[:limit]
        # Счётчики отдельными запросами: COUNT по нескольким JOIN сразу перемножает строки.
        counts = {
            label: dict(
                model.objects.filter(user_id__in=preview_ids).values("user_id")
                .annotate(n=Count("id")).values_list("user_id", "n")
            )
            for label, model in (
                ("сессий", EvaluationSession), ("оценок игроков", PlayerEvaluation), ("прогнозов", MatchPrediction),
            )
        }
        for user in User.objects.filter(id__in=preview_ids).order_by("date_joined"):
            parts = ", ".join(f"{label}: {by_user.get(user.id, 0)}" for label, by_user in counts.items())
            self.stdout.write(f"  · {user.username} <{user.email}> — {parts}")
        if limit and len(user_ids) > limit:
            self.stdout.write(f"  … и ещё {len(user_ids) - limit}")

        match_ids = self._affected_match_ids(user_ids)
        totals = {model.__name__: model.objects.filter(user_id__in=user_ids).count() for model in BULK_MODELS}
        self.stdout.write(f"\nАккаунтов: {len(user_ids)}, затронуто матчей: {len(match_ids)}")
        for name, n in totals.items():
            if n:
                self.stdout.write(f"  {name}: {n}")

        if not options["apply"]:
            self.stdout.write(self.style.NOTICE("dry-run: ничего не удалено. Запустите с --apply."))
            return

        self._delete(user_ids)

        if options["reset_corrections"]:
            self._reset_corrections()

        if options["recalc_all"]:
            match_ids |= self._all_rated_match_ids()

        if options["no_recalc"]:
            self.stdout.write(self.style.WARNING(
                "Пересчёт пропущен (--no-recalc). Рейтинги затронутых матчей пока содержат голоса ботов."
            ))
            return
        self._recalculate(match_ids)

    def _delete(self, user_ids) -> None:
        """Крупные таблицы — прямым DELETE пачками (без загрузки строк и сигналов пересчёта
        на каждую оценку), остальное — обычным каскадом вместе с аккаунтами.
        """
        from analytics.models import AnalyticsEvent

        for start in range(0, len(user_ids), DELETE_BATCH_USERS):
            batch = user_ids[start:start + DELETE_BATCH_USERS]
            with transaction.atomic():
                for model in BULK_MODELS:
                    qs = model.objects.filter(user_id__in=batch)
                    qs._raw_delete(qs.db)
                # SET_NULL оставил бы «анонимные» события ботов в воронках — удаляем явно.
                analytics_qs = AnalyticsEvent.objects.filter(user_id__in=batch)
                analytics_qs._raw_delete(analytics_qs.db)
                User.objects.filter(id__in=batch).delete()
            self.stdout.write(f"  удалено аккаунтов: {min(start + DELETE_BATCH_USERS, len(user_ids))}/{len(user_ids)}")
        self.stdout.write(self.style.SUCCESS(f"Удалено аккаунтов: {len(user_ids)} со всеми данными."))

    def _all_rated_match_ids(self) -> set:
        """Матчи, где есть хоть один агрегат или оценка (устаревшие агрегаты тоже пересчитаются)."""
        from aggregates.models import (
            CoachMatchAggregate, MatchAggregate, PlayerMatchAggregate, RefereeMatchAggregate, TeamMatchAggregate,
        )

        match_ids: set = set()
        # Прогнозы не в счёт: иначе будущим матчам создались бы пустые агрегаты.
        rating_models = [m for m in BULK_MODELS if m is not MatchPrediction]
        for model in (PlayerMatchAggregate, TeamMatchAggregate, CoachMatchAggregate, RefereeMatchAggregate,
                      MatchAggregate, *rating_models):
            match_ids.update(model.objects.values_list("match_id", flat=True).distinct())
        return match_ids

    def _affected_match_ids(self, user_ids) -> set:
        match_ids: set = set()
        for model in BULK_MODELS:
            if model is MatchPrediction:
                continue  # прогнозы не влияют на рейтинги
            match_ids.update(
                model.objects.filter(user_id__in=user_ids).values_list("match_id", flat=True).distinct()
            )
        return match_ids

    def _reset_corrections(self) -> None:
        from aggregates.models import PlayerMatchAggregate, PlayerRatingCorrection, TeamMatchAggregate, TeamRatingCorrection
        from aggregates.tasks import strip_applied_corrections

        player_ids = list(PlayerRatingCorrection.objects.values_list("player_id", flat=True))
        team_ids = list(TeamRatingCorrection.objects.values_list("team_id", flat=True))
        PlayerRatingCorrection.objects.update(correction=0.0, last_pattern="")
        TeamRatingCorrection.objects.update(correction=0.0, last_pattern="")
        stripped = strip_applied_corrections(PlayerMatchAggregate, "player_id", player_ids)
        stripped += strip_applied_corrections(TeamMatchAggregate, "team_id", team_ids)
        # Необработанные сигналы про сущности тоже посчитаны по старым голосам — детекторы пересоздадут актуальные.
        from users.models import SuspiciousActivityFlag

        flags_deleted, _ = SuspiciousActivityFlag.objects.filter(
            status="pending", user__isnull=True,
            source__in=("vote_spike", "stats_divergence", "player_stats_divergence", "coach_stats_divergence"),
        ).delete()
        self.stdout.write(
            f"Авто-поправки обнулены, снято с {stripped} матчевых рейтингов; "
            f"удалено необработанных сигналов по сущностям: {flags_deleted}."
        )

    def _recalculate(self, match_ids) -> None:
        """Синхронно: агрегаты матчей, затем сборные сезонов и туры."""
        from aggregates import tasks as agg_tasks
        from matches.models import Match
        from round_squad.models import RoundBestXI
        from round_squad.services import recompute_round
        from season_squad.services import recompute_best_xi
        from seasons.models import Season

        for n, match_id in enumerate(sorted(match_ids, key=str), start=1):
            agg_tasks.recalculate_match_now(match_id)
            if n % 50 == 0:
                self.stdout.write(f"  пересчитано матчей: {n}/{len(match_ids)}")
        self.stdout.write(f"Агрегаты пересчитаны: {len(match_ids)} матчей.")

        season_ids = set(Match.objects.filter(id__in=match_ids).values_list("season_id", flat=True))
        for season in Season.objects.filter(id__in=season_ids).select_related("league"):
            recompute_best_xi(season, force=True)
        self.stdout.write(f"Сборные сезонов пересчитаны: {len(season_ids)}.")

        tour_pairs = set(
            Match.objects.filter(id__in=match_ids, tour__isnull=False).values_list("season_id", "tour")
        )
        # И зафиксированные, и живые туры — живые иначе ждали бы планового пересчёта.
        rounds = RoundBestXI.objects.select_related("season__league")
        recomputed = 0
        for round_xi in rounds:
            if (round_xi.season_id, round_xi.tour) in tour_pairs:
                recompute_round(round_xi.season, round_xi.tour, force=True)
                recomputed += 1
        self.stdout.write(self.style.SUCCESS(f"Туры пересчитаны: {recomputed}. Готово."))
