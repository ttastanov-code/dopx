# core/management/commands/reset_ratings_data.py
"""
manage.py reset_ratings_data [--apply] [--keep-user EMAIL_OR_USERNAME]

Точечная чистка "мусора" от голосований/тестовых оценок, накопленного за
цикл разработки (2026-09-01, продуктовый запрос: "почистить базу от оценок
и агрегатов") — БЕЗ полного `manage.py flush`. Полный flush снёс бы заодно
матчи/команды/игроков/составы, которые долго и с трудом синкались из
хрупкого KFF API (см. вся эта переписка про 403/circuit breaker) — их
пришлось бы полностью пересинкать с нуля. Эта команда трогает только
голоса и производные от них рейтинги, оставляя справочные данные
(League/Season/Team/Player/Coach/Referee/Match/составы) как есть.

--keep-user — исключить оценки ОДНОГО пользователя (свои — "мои могут
оставаться") из удаления. Остальные пользователи чистятся полностью.

Удаляет:
  · evaluations.* — ContextEvaluation, TeamEvaluation, PlayerEvaluation,
    CoachEvaluation, RefereeEvaluation, MatchEvaluation, EvaluationSession
    (сами голоса и сессии вайзарда) — везде, КРОМЕ --keep-user, если задан.
  · aggregates.* — PlayerMatchAggregate, CoachMatchAggregate,
    TeamMatchAggregate, RefereeMatchAggregate, MatchAggregate,
    TeamRatingCorrection — ВСЕГДА целиком, без исключений. У этих моделей
    нет поля user (это агрегат по МАТЧУ, а не по голосующему), выборочно
    оставить тут нечего — пересчитываются заново из оставшихся оценок
    (`manage.py recalculate_aggregates`, либо дождаться планового прогона).

НАЙДЕНО (2026-09-01, жалоба пользователя: "удалял в админке сессии
голосования и проходил заново, могут быть дубликаты"): дубликатов СТРОК в
evaluations быть не может — везде unique_together(user, match[, entity]) +
update_or_create (см. evaluations/views.py), повторное прохождение вайзарда
просто ПЕРЕЗАПИСЫВАЕТ те же строки новыми значениями, а не плодит копии.
РЕАЛЬНАЯ порча — в счётчиках на User/UserXP, потому что удаление
EvaluationSession в админке в обход штатного flow (evaluations/views.py:
`if session.status == 'completed': ... redirect`) снимает единственную
защиту от повторного начисления: `update_evaluation_stats()`
(total_evaluations/evaluation_streak — чистые аккумуляторы, +1 при КАЖДОМ
вызове, не count() по факту) и `UserXP.add_xp()` на последнем шаге вайзарда
срабатывают заново при каждом повторном прохождении одного и того же матча.
С --keep-user эта команда сама пересчитывает total_evaluations/
evaluation_streak/last_evaluation_season_id/last_evaluation_tour у
оставленного пользователя ЗАНОВО из его реальных MatchEvaluation (по
`created_at` — auto_now_add, не трогается повторным update_or_create,
значит порядок первого прохождения каждого матча восстановим корректно,
несмотря на все передвижения).

total_xp/trust_score НЕ пересчитываются автоматически — в отличие от
total_evaluations/evaluation_streak (чистая функция от списка матчей),
их формулы зависят от `xp_multiplier()`/`trust_score` НА МОМЕНТ каждого
начисления, то есть корректно "переиграть" всю историю задним числом
нельзя — только заново решить, какое значение считать правильным (сбросить
на дефолт 1.0 / обнулить и дать накопиться заново на реальных данных /
оставить как есть). Сознательно не гадаю тут — команда только предупреждает
в конце, финальное решение за вами.

НЕ трогает (сознательно, отдельным решением, не по умолчанию):
  · UserBadge — не все бейджи про количество оценок (founder/
    monthly_champion и т.п. — про другое), огульно удалять нельзя.
  · predictions.* — отдельная фича, не оценки выступления.
  · round_squad/season_squad ("Сборная тура/сезона") — снэпшот без FK на
    aggregates, не каскадит, но покажет старые составы до пересчёта:
    `manage.py shell -c "from round_squad.tasks import recompute_active_rounds; recompute_active_rounds()"`
    (аналогично `season_squad.tasks.recompute_all_active_best_xi`).

Без --apply — dry-run: только считает и печатает количество строк по
каждой модели, ничего не удаляет (тот же паттерн, что у
cleanup_test_users.py/clear_player_photos.py).
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

# Порядок важен только для читаемости вывода — все FK здесь CASCADE,
# Django сам разрулит порядок реального удаления в транзакции. Все модели
# ниже имеют поле `user` — фильтр --keep-user (`exclude(user=keep_user)`)
# применим к каждой одинаково.
EVALUATION_MODELS = [
    ContextEvaluation, TeamEvaluation, PlayerEvaluation,
    CoachEvaluation, RefereeEvaluation, MatchEvaluation, EvaluationSession,
]
AGGREGATE_MODELS = [
    PlayerMatchAggregate, CoachMatchAggregate, TeamMatchAggregate,
    RefereeMatchAggregate, MatchAggregate, TeamRatingCorrection,
]


def _recompute_evaluation_stats(user) -> None:
    """
    Пересчитывает total_evaluations/evaluation_streak/
    last_evaluation_season_id/last_evaluation_tour у `user` заново, реплеем
    того же алгоритма, что и `User.update_evaluation_stats()` (users/models.py),
    но по РЕАЛЬНЫМ оставшимся MatchEvaluation в порядке `created_at`
    (auto_now_add — фиксируется при первой оценке этого матча, повторное
    прохождение вайзарда через update_or_create его не сдвигает). Это и
    восстанавливает корректную серию/счётчик независимо от того, сколько
    раз матч переоценивался.
    """
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
            continue  # как и в оригинале — нет тура, серию не трогаем
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
