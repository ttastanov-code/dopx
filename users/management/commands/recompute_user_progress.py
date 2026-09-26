# users/management/commands/recompute_user_progress.py
"""manage.py recompute_user_progress [--user USERNAME] [--apply]

Пересобирает XP/уровень, счётчик и серии оценок и прогнозов, достижения
из фактических данных (завершённые сессии, прогнозы). Без --apply — dry-run:
показывает, что изменится, и откатывает транзакцию.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from users.models import User
from users.progress import recompute_user_progress


class _DryRun(Exception):
    pass


class Command(BaseCommand):
    help = "Пересчитать XP, серии и достижения пользователей из фактических данных"

    def add_arguments(self, parser):
        parser.add_argument("--user", help="username; по умолчанию — все активные")
        parser.add_argument("--apply", action="store_true", help="Записать изменения.")

    def handle(self, *args, **options):
        users = User.objects.filter(is_active=True)
        if options["user"]:
            users = users.filter(username=options["user"])
        changed = 0
        for user in users.order_by("username"):
            try:
                with transaction.atomic():
                    summary = recompute_user_progress(user)
                    if not options["apply"]:
                        raise _DryRun
            except _DryRun:
                pass
            if self._differs(summary):
                changed += 1
                self.stdout.write(f"{user.username}: {self._format(summary)}")
        mode = "применено" if options["apply"] else "dry-run, --apply чтобы записать"
        self.stdout.write(self.style.SUCCESS(f"Изменится у {changed} из {users.count()} ({mode})"))

    @staticmethod
    def _differs(s: dict) -> bool:
        return s["before"] != s["after"] or s["xp"][0] != s["xp"][1] or s["badges_removed"] or s["badges_added"]

    @staticmethod
    def _format(s: dict) -> str:
        b, a = s["before"], s["after"]
        parts = [
            f"оценок {b['total_evaluations']}→{a['total_evaluations']}",
            f"серия {b['evaluation_streak']}→{a['evaluation_streak']}",
            f"прогнозы {b['prediction_streak']}→{a['prediction_streak']}",
            f"XP {s['xp'][0]}→{s['xp'][1]} (ур. {s['level'][0]}→{s['level'][1]})",
        ]
        if s["badges_removed"]:
            parts.append("снято: " + ", ".join(s["badges_removed"]))
        if s["badges_added"]:
            parts.append("выдано: " + ", ".join(s["badges_added"]))
        return "; ".join(parts)
