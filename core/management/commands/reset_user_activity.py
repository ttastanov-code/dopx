# core/management/commands/reset_user_activity.py
"""manage.py reset_user_activity [--apply] [--delete-staff] [--keep-user LOGIN ...]

«Чистый старт» без flush: удаляет всё, что создали пользователи, и всё, что из этого посчитано.
Удаляет: аккаунты (кроме сотрудников и --keep-user), оценки и сессии, рейтинги (агрегаты
и поправки), сборные тура и сезона, прогнозы, реакции, достижения, уведомления, обращения,
аналитику, вовлечение (серии, задания, абонемент, лиги, приглашения, опросы), антифрод-флаги,
логи входа, share-карточки в media.

Не трогает: лиги, сезоны, команды, игроков, тренеров, судей, матчи, события, составы,
статистику (Sportmonks), правки ФИО (Gemini), настройки платформы, аудит, рекламу, мнения экспертов.

У оставленных аккаунтов активность тоже обнуляется: XP, уровень, серии, доверие.
  --delete-staff   удалить и сотрудников (потом: createsuperuser)
  --keep-user      логин или email, чей аккаунт оставить (можно несколько раз)
Без --apply — dry-run: только счётчики.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

# (app_label.Model, подпись) — удаляются целиком.
WIPE_MODELS = [
    ("evaluations.PlayerEvaluation", "оценки игроков"),
    ("evaluations.TeamEvaluation", "оценки команд"),
    ("evaluations.CoachEvaluation", "оценки тренеров"),
    ("evaluations.RefereeEvaluation", "оценки судей"),
    ("evaluations.MatchEvaluation", "оценки матчей"),
    ("evaluations.ContextEvaluation", "контекст оценок"),
    ("evaluations.EvaluationSession", "сессии оценки"),
    ("aggregates.PlayerMatchAggregate", "рейтинги игроков"),
    ("aggregates.CoachMatchAggregate", "рейтинги тренеров"),
    ("aggregates.TeamMatchAggregate", "рейтинги команд"),
    ("aggregates.RefereeMatchAggregate", "рейтинги судей"),
    ("aggregates.MatchAggregate", "рейтинги матчей"),
    ("aggregates.PlayerRatingCorrection", "поправки игроков"),
    ("aggregates.TeamRatingCorrection", "поправки команд"),
    ("round_squad.RoundPositionRanking", "рейтинги позиций тура"),
    ("round_squad.RoundBestXISlot", "слоты сборных тура"),
    ("round_squad.RoundBestXI", "сборные тура"),
    ("season_squad.SeasonPositionRanking", "рейтинги позиций сезона"),
    ("season_squad.SeasonBestXISlot", "слоты сборных сезона"),
    ("season_squad.SeasonBestXI", "сборные сезона"),
    ("predictions.MatchPrediction", "прогнозы"),
    ("matches.MatchReaction", "реакции на матчи"),
    ("events.EventReaction", "реакции на события"),
    ("users.UserBadge", "достижения"),
    ("users.SuspiciousActivityFlag", "антифрод-флаги"),
    ("notifications.Notification", "уведомления"),
    ("notifications.ContactSubmission", "обращения"),
    ("analytics.AnalyticsEvent", "события аналитики"),
    ("engagement.DailyPollVote", "голоса опросов"),
    ("engagement.DailyPoll", "опросы недели"),
    ("engagement.DailyQuest", "задания дня"),
    ("engagement.DailyStreak", "серии дней"),
    ("engagement.SeasonPass", "абонементы"),
    ("engagement.FriendLeagueMember", "участники лиг"),
    ("engagement.FriendLeague", "лиги с друзьями"),
    ("engagement.Referral", "приглашения"),
    ("engagement.ReferralCode", "коды приглашений"),
    ("axes.AccessAttempt", "попытки входа"),
    ("axes.AccessLog", "журнал входов"),
    ("axes.AccessFailureLog", "неудачные входы"),
]


class Command(BaseCommand):
    help = "Удалить всю пользовательскую активность, сохранив данные API и правки ФИО."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--delete-staff", action="store_true")
        parser.add_argument("--keep-user", action="append", default=[])

    def handle(self, *args, apply, delete_staff, keep_user, **options):
        from django.apps import apps

        from users.models import User

        keep_q = Q(pk__in=[])
        if not delete_staff:
            keep_q |= Q(is_staff=True) | Q(is_superuser=True)
        for login in keep_user:
            found = User.objects.filter(Q(username=login) | Q(email__iexact=login))
            if not found.exists():
                raise CommandError(f"--keep-user: пользователь «{login}» не найден")
            keep_q |= Q(pk__in=found.values("pk"))

        kept = User.objects.filter(keep_q)
        doomed = User.objects.exclude(pk__in=kept.values("pk"))
        self.stdout.write(f"Аккаунты: удалить {doomed.count()}, оставить {kept.count()} "
                          f"({', '.join(kept.values_list('username', flat=True)[:10]) or '—'})")

        plan = []
        for label, title in WIPE_MODELS:
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            plan.append((model, title, model.objects.count()))
        for _model, title, count in plan:
            if count:
                self.stdout.write(f"  {title}: {count}")

        if not apply:
            self.stdout.write(self.style.NOTICE("Dry-run: ничего не удалено. Добавьте --apply."))
            return

        with transaction.atomic():
            for model, _title, _count in plan:
                model.objects.all().delete()
            deleted_users = doomed.count()
            doomed.delete()
            self._reset_kept(kept)
        cards = self._drop_share_cards()

        from django.core.cache import cache

        from core.live import bump_data_version

        cache.clear()
        bump_data_version()
        self.stdout.write(self.style.SUCCESS(
            f"Готово: удалено аккаунтов {deleted_users}, share-карточек {cards}. "
            f"Каталог (Sportmonks) и правки ФИО не тронуты."
        ))
        if delete_staff:
            self.stdout.write("Создайте администратора: manage.py createsuperuser")

    def _reset_kept(self, kept):
        from users.models import UserXP

        kept.update(
            total_evaluations=0, evaluation_streak=0, prediction_streak=0,
            last_evaluation_season_id=None, last_evaluation_tour=None, trust_score=1.0,
        )
        UserXP.objects.filter(user__in=kept).update(total_xp=0, xp_remainder=0.0, level=1)

    def _drop_share_cards(self) -> int:
        """Картинки для шеринга строятся из рейтингов — после сброса они устарели."""
        from django.core.files.storage import default_storage

        try:
            _dirs, files = default_storage.listdir("share-cards")
        except (FileNotFoundError, NotImplementedError, OSError):
            return 0
        for name in files:
            default_storage.delete(f"share-cards/{name}")
        return len(files)
