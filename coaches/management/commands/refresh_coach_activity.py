# coaches/management/commands/refresh_coach_activity.py
"""
Синхронная обёртка над coaches/services.py::refresh_coach_activity() —
та же логика, что кнопка "Обновить статус тренеров" на /staff/dashboard/
parser/ (dashboard/parser_tools.py::SPORTMONKS_TRIGGERABLE_TASKS ->
parsers.sportmonks.tasks.sportmonks_sync_coach_activity), но выполняется
СРАЗУ в текущем процессе, а не ставится в очередь Celery.

ЗАЧЕМ ЭТА КОМАНДА (2026-09-09, жалоба пользователя — "статусы тренеров
обновлял через команду в дашборде, но ничего не случилось"): кнопка на
дашборде вызывает `task_fn.delay()` (dashboard/parser_tools.py::
trigger_task) — это ставит задачу в очередь Celery (Redis) и требует
ОТДЕЛЬНОГО запущенного воркера (`celery -A dopx worker`), чтобы она
реально выполнилась. Если локально запущен только `manage.py runserver`
без воркера — задача тихо лежит в очереди и никогда не исполняется, кнопка
при этом не сообщает об ошибке (она и не ошиблась — просто поставила в
очередь, как и было запрограммировано). Не баг логики пересчёта самого по
себе, а несоответствие ожиданиям в локальной разработке без воркера. Эта
команда — прямой путь получить результат немедленно, без Celery вообще
(тот же принцип, что sync_sportmonks_season для полного бэкафилла).

Использование:
    python manage.py refresh_coach_activity
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
