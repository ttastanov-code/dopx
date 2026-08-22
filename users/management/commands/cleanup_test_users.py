# users/management/commands/cleanup_test_users.py
"""
manage.py cleanup_test_users [--apply] [--prefix test_user] [--domain test.dopx.kz]

Точечная чистка тестовых аккаунтов и их данных ПОСЛЕ раунда тестирования
(нагрузочного или ручного) — без полного flush всей БД (продуктовый запрос
2026-08-22: "загнать тестовых пользователей, сделать тестовые голосования,
а по завершению тестов очистить БД от тестовых голосов и пользователей, не
чистя всё"). Раньше эта команда удаляла ВСЕХ non-staff/non-superuser
пользователей без разбора — это ломало реальные аккаунты, если к моменту
чистки на сайте уже завелись настоящие первые пользователи. Теперь критерий
сужен до опознаваемых тестовых аккаунтов.

Критерий "тестовый" — совпадение ЛЮБОГО из условий:
  · username начинается с --prefix (по умолчанию 'test_user' — тот же
    префикс, что create_test_users.py и create_test_evaluations.py
    используют при создании: 'test_user_...')
  · email заканчивается на @--domain (по умолчанию 'test.dopx.kz' —
    домен из create_test_users.py; create_test_evaluations.py по
    привычке иногда сажает на @test.com, поэтому email-фильтр смотрит
    на любой домен, начинающийся с 'test.')

Staff/superuser никогда не попадают под удаление независимо от имени —
доп. страховка на случай, если кто-то создаст staff-аккаунт с тестовым
префиксом вручную.

Удаление User каскадно чистит все связанные данные (PlayerEvaluation/
MatchEvaluation/TeamEvaluation/CoachEvaluation/RefereeEvaluation/
ContextEvaluation, UserBadge/UserXP, EvaluationSession, Follow,
PushSubscription, Notification, Prediction — везде on_delete=CASCADE на
пользователя, см. users/models.py, evaluations/models.py, notifications/
models.py, predictions/models.py; исключение — AnalyticsEvent.user и
dashboard.StaffAuditLog.user, там on_delete=SET_NULL, события/лог остаются
анонимными записями, а не удаляются). Это и есть причина, по которой после
чистки тестовых пользователей стоит запустить пересчёт агрегатов и «Сборной
DOPX» вручную (dashboard → Парсер → «Сборная DOPX: пересчитать сейчас», или
manage.py recalculate_aggregates для конкретных матчей) — иначе рейтинги
будут отражать удалённые тестовые голоса ещё до следующего планового
пересчёта.

Без --apply — dry-run: только считает и печатает список, ничего не
удаляет (тот же паттерн, что у clear_player_photos.py и
backfill_player_positions.py).

Полный flush всей БД (manage.py flush) остаётся отдельным, более тяжёлым
инструментом для "почистить вообще всё перед боевым запуском" — эта
команда для многократного цикла "насеяли тестовых → проверили → убрали
только тестовое" без риска задеть реальные данные.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count, Q

User = get_user_model()


class Command(BaseCommand):
    help = "Удаляет опознанных тестовых пользователей (по префиксу username/домену email) и их данные"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить пользователей")
        parser.add_argument(
            "--prefix", type=str, default="test_user",
            help="Префикс username, считающийся тестовым (по умолчанию 'test_user')",
        )
        parser.add_argument(
            "--domain", type=str, default="test.dopx.kz",
            help="Домен email, считающийся тестовым (по умолчанию 'test.dopx.kz'); "
                 "также всегда matчится любой email вида *@test.*",
        )
        parser.add_argument(
            "--limit-preview", type=int, default=50,
            help="Сколько строк списка печатать в dry-run (по умолчанию 50, 0 — все)",
        )

    def handle(self, *args, **options):
        apply_changes: bool = options["apply"]
        prefix: str = options["prefix"]
        domain: str = options["domain"]
        limit_preview: int = options["limit_preview"]

        candidates = (
            User.objects.filter(is_staff=False, is_superuser=False)
            .filter(
                Q(username__startswith=prefix)
                | Q(email__iendswith=f"@{domain}")
                | Q(email__iregex=r"@test\.")
            )
            .annotate(
                player_evals=Count("player_evaluations", distinct=True),
                match_evals=Count("match_evaluations", distinct=True),
            )
            .order_by("date_joined")
        )
        count = candidates.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS(
                f"Тестовых пользователей не найдено (критерий: username^'{prefix}' "
                f"или email@{domain} или email@test.*)."
            ))
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
                f"\ndry-run: под удаление попадёт {count} тестовых пользователей "
                f"и все их данные (оценки, XP, бейджи, прогнозы, подписки, уведомления). "
                f"Реальные аккаунты (не под критерий тестовых) не затрагиваются. "
                f"Запустите с --apply, чтобы удалить."
            ))
            return

        deleted_count, _details = candidates.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Готово — удалено {count} тестовых пользователей ({deleted_count} строк во всех "
            f"связанных таблицах суммарно). Реальные пользователи не затронуты."
        ))
        self.stdout.write(self.style.WARNING(
            "Не забудьте пересчитать агрегаты/«Сборную DOPX» (dashboard → Парсер → "
            "«Сборная DOPX: пересчитать сейчас», или manage.py recalculate_aggregates) — "
            "иначе рейтинги до планового пересчёта ещё будут учитывать удалённые тестовые голоса."
        ))
