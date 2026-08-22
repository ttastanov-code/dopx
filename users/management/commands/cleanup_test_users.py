# users/management/commands/cleanup_test_users.py
"""
manage.py cleanup_test_users [--apply]

Разовая чистка тестовых аккаунтов и их данных перед боевым запуском
(продуктовый запрос 2026-08-22). Критерий — ЛЮБОЙ пользователь без
is_staff/is_superuser: сюда попадут и аккаунты, созданные через
create_test_users.py (username test_user_*@test.dopx.kz — см. этот файл,
он использовался для нагрузочного тестирования), и любые обычные
аккаунты, зарегистрированные вручную через сайт для проверки фич
(например, "Сборной DOPX").

ВАЖНО, сознательный выбор при постановке задачи: критерий НЕ фильтрует
по email/username-паттерну — удаляются ВСЕ non-staff пользователи без
исключения. Если к моменту запуска на сайте уже есть настоящие первые
пользователи (не staff), --apply удалит и их данные тоже. Поэтому
dry-run (команда без --apply) печатает ПОЛНЫЙ список кандидатов — прежде
чем применять, стоит проверить его глазами, а не полагаться на то, что
там только тестовые записи.

Удаление User каскадно чистит все связанные данные (PlayerEvaluation/
MatchEvaluation/CoachEvaluation, UserBadge/UserXP, EvaluationSession,
Follow, PushSubscription, Notification, Prediction — везде
on_delete=CASCADE на пользователя, см. users/models.py, evaluations/
models.py, notifications/models.py, predictions/models.py; исключение —
AnalyticsEvent.user и dashboard.StaffAuditLog.user, там on_delete=SET_NULL,
события/лог остаются анонимными записями, а не удаляются). Это и есть
причина, по которой после чистки тестовых пользователей стоит запустить
пересчёт «Сборной DOPX» вручную (dashboard → Парсер → «Сборная DOPX:
пересчитать сейчас») — иначе состав будет отражать удалённые оценки ещё
до следующего планового пересчёта по расписанию.

Без --apply — dry-run: только считает и печатает список, ничего не
удаляет (тот же паттерн, что у clear_player_photos.py и
backfill_player_positions.py).
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count

User = get_user_model()


class Command(BaseCommand):
    help = "Удаляет всех non-staff/non-superuser пользователей и их данные (оценки, XP, бейджи, прогнозы и т.д.)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить пользователей")
        parser.add_argument(
            "--limit-preview", type=int, default=50,
            help="Сколько строк списка печатать в dry-run (по умолчанию 50, 0 — все)",
        )

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]
        limit_preview: int = options["limit_preview"]

        candidates = (
            User.objects.filter(is_staff=False, is_superuser=False)
            .annotate(
                player_evals=Count("player_evaluations", distinct=True),
                match_evals=Count("match_evaluations", distinct=True),
            )
            .order_by("date_joined")
        )
        count = candidates.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("Non-staff пользователей нет — нечего чистить."))
            return

        rows = candidates if limit_preview == 0 else candidates[:limit_preview]
        for user in rows:
            self.stdout.write(
                f"  · {user.username} <{user.email}> — рег. {user.date_joined:%Y-%m-%d}, "
                f"оценок матчей: {user.player_evals}, оценок игр: {user.match_evals}"
            )
        if limit_preview and count > limit_preview:
            self.stdout.write(f"  … и ещё {count - limit_preview}")

        if not apply_changes:
            self.stdout.write(self.style.NOTICE(
                f"\ndry-run: под удаление попадёт {count} пользователей (все non-staff/non-superuser) "
                f"и все их данные (оценки, XP, бейджи, прогнозы, подписки, уведомления). "
                f"Проверьте список выше — команда не различает тестовые и настоящие аккаунты. "
                f"Запустите с --apply, чтобы удалить."
            ))
            return

        deleted_count, _details = candidates.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Готово — удалено {count} пользователей ({deleted_count} строк во всех связанных таблицах суммарно)."
        ))
        self.stdout.write(self.style.WARNING(
            "Не забудьте вручную пересчитать «Сборную DOPX» (dashboard → Парсер → "
            "«Сборная DOPX: пересчитать сейчас») — иначе состав до планового пересчёта "
            "ещё будет учитывать удалённые оценки."
        ))
