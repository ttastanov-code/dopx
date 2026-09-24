# core/management/commands/reset_ratings_data.py
"""manage.py reset_ratings_data [--apply] [--keep-user EMAIL_OR_USERNAME]

Удаляет оценки и агрегаты, справочные данные и матчи не трогает.
  evaluations.* — всё, кроме оценок --keep-user;
  aggregates.*  — всё (пересчитать: recalculate_aggregates).
Для --keep-user пересчитывает total_evaluations/evaluation_streak по его MatchEvaluation.
total_xp/trust_score, бейджи, прогнозы, сборные — не трогает.
Без --apply — dry-run.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from aggregates.models import (
    CoachMatchAggregate,
    MatchAggregate,
    PlayerMatchAggregate,
    RefereeMatchAggregate,
    TeamMatchAggregate,
    TeamRatingCorrection,
)
from evaluations.models import (
    ContextEvaluation,
    CoachEvaluation,
    EvaluationSession,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)

User = get_user_model()

# Порядок — только для вывода. У всех моделей есть user.
EVALUATION_MODELS = [
    ContextEvaluation, TeamEvaluation, PlayerEvaluation,
    CoachEvaluation, RefereeEvaluation, MatchEvaluation, EvaluationSession,
]
AGGREGATE_MODELS = [
    PlayerMatchAggregate, CoachMatchAggregate, TeamMatchAggregate,
    RefereeMatchAggregate, MatchAggregate, TeamRatingCorrection,
]


def _recompute_evaluation_stats(user) -> None:
    """Пересчёт счётчиков оценок пользователя по MatchEvaluation в порядке created_at."""
    evaluations = (
        MatchEvaluation.objects.filter(user=user)
        .select_related("match")
        .order_by("created_at")
    )
    total = 0
    streak = 0
    last_season_id = None
    last_tour = None
    for ev in evaluations:
        total += 1
        tour = ev.match.tour
        if tour is None:
            continue  # нет тура — серию не трогаем
        if last_season_id == ev.match.season_id and last_tour == tour:
            continue
        elif (
            last_season_id == ev.match.season_id
            and last_tour is not None
            and tour == last_tour + 1
        ):
            streak += 1
        else:
            streak = 1
        last_season_id = ev.match.season_id
        last_tour = tour

    user.total_evaluations = total
    user.evaluation_streak = streak
    user.last_evaluation_season_id = last_season_id
    user.last_evaluation_tour = last_tour
    user.save(update_fields=[
        "total_evaluations", "evaluation_streak",
        "last_evaluation_season_id", "last_evaluation_tour", "updated_at",
    ])


class Command(BaseCommand):
    help = "Удаляет все оценки (evaluations) и посчитанные из них рейтинги (aggregates), не трогая матчи/команды/игроков/пользователей."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить (без флага — только dry-run подсчёт)")
        parser.add_argument(
            "--keep-user", type=str, default=None,
            help="Username или email пользователя, чьи оценки НЕ удалять (остальные — чистятся полностью)",
        )

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]
        keep_user_ref: str | None = options["keep_user"]

        keep_user = None
        if keep_user_ref:
            keep_user = User.objects.filter(
                Q(username=keep_user_ref) | Q(email__iexact=keep_user_ref)
            ).first()
            if keep_user is None:
                raise CommandError(f"Пользователь '{keep_user_ref}' не найден (ни по username, ни по email).")
            self.stdout.write(f"Сохраняем оценки пользователя: {keep_user.username} <{keep_user.email}>\n")

        def eval_qs(model):
            qs = model.objects.all()
            return qs.exclude(user=keep_user) if keep_user else qs

        self.stdout.write("Оценки (evaluations):")
        eval_counts = {}
        for model in EVALUATION_MODELS:
            count = eval_qs(model).count()
            eval_counts[model] = count
            if count:
                self.stdout.write(f"  {model._meta.label}: {count}")

        self.stdout.write("Агрегаты/рейтинги (aggregates) — удаляются целиком, без исключений:")
        agg_counts = {}
        for model in AGGREGATE_MODELS:
            count = model.objects.count()
            agg_counts[model] = count
            if count:
                self.stdout.write(f"  {model._meta.label}: {count}")

        total = sum(eval_counts.values()) + sum(agg_counts.values())
        if total == 0:
            self.stdout.write(self.style.SUCCESS("Нечего чистить — оценок и агрегатов в базе нет."))
            return

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                f"\ndry-run: будет удалено {total} строк суммарно. "
                f"Матчи/команды/игроки/пользователи/прогнозы НЕ затрагиваются. "
                f"Запустите с --apply, чтобы удалить."
            ))
            return

        with transaction.atomic():
            for model in EVALUATION_MODELS:
                if eval_counts[model]:
                    eval_qs(model).delete()
            for model in AGGREGATE_MODELS:
                if agg_counts[model]:
                    model.objects.all().delete()
            if keep_user:
                _recompute_evaluation_stats(keep_user)

        self.stdout.write(self.style.SUCCESS(f"Готово — удалено {total} строк (оценки + агрегаты)."))
        if keep_user:
            keep_user.refresh_from_db()
            self.stdout.write(self.style.SUCCESS(
                f"У {keep_user.username} пересчитаны total_evaluations={keep_user.total_evaluations}, "
                f"evaluation_streak={keep_user.evaluation_streak} — по факту оставшихся оценок, "
                f"без учёта повторных прохождений вайзарда."
            ))
            self.stdout.write(self.style.WARNING(
                f"total_xp/trust_score у {keep_user.username} НЕ тронуты — их нельзя корректно "
                f"пересчитать задним числом (формулы зависят от значений НА МОМЕНТ каждого "
                f"начисления). Если из-за повторных прохождений вайзарда они завышены — "
                f"решите сами: сбросить на дефолт (XP=0, trust_score=1.0) или оставить как есть."
            ))
        self.stdout.write(self.style.WARNING(
            "Сборная тура/сезона (round_squad/season_squad) не пересчитается сама — "
            "если показывает старые составы, пересчитай вручную (см. докстринг команды)."
        ))
        self.stdout.write(self.style.WARNING(
            "UserBadge не тронут — не все бейджи привязаны к количеству оценок (founder, "
            "monthly_champion и т.п.). Если нужно почистить и его — отдельным точечным шагом."
        ))
