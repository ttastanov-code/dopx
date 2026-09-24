# coaches/management/commands/refresh_coach_activity.py
"""manage.py refresh_coach_activity

Синхронный пересчёт активности тренеров (coaches.services.refresh_coach_activity),
без Celery — кнопка в дашборде требует запущенного воркера.
"""
from django.core.management.base import BaseCommand

from coaches.services import refresh_coach_activity


class Command(BaseCommand):
    help = "Пересчитывает is_active у тренеров по последнему сыгранному матчу их команды (без Celery, выполняется сразу)"

    def handle(self, *args, **options):
        result = refresh_coach_activity()
        self.stdout.write(self.style.SUCCESS(
            f"Команд проверено: {result['teams_checked']}, "
            f"деактивировано тренеров: {result['deactivated']}, "
            f"реактивировано: {result['reactivated']}"
        ))
        if result["deactivated"] == 0 and result["reactivated"] == 0:
            self.stdout.write(
                "Изменений не было — либо все статусы уже верны, либо у "
                "проблемных команд последний сыгранный матч не заполнил "
                "home_coach/away_coach (тогда все тренеры этой команды "
                "были бы деактивированы разом — проверьте вручную конкретную "
                "команду через /admin/matches/match/, если ожидали иначе)."
            )
