# teams/signals.py
"""
post_save(Team) -> при первом сохранении команды с логотипом, но без ещё
посчитанного фирменного цвета, ставит в очередь compute_team_primary_color_task
(teams/tasks.py). Это делает функцию "автоцвет клубов" полностью
автоматической: ни для команд, добавленных вручную через админку, ни для
команд, которые создаёт парсер KFF при импорте (parsers/kff/importers.py),
не нужно руками запускать management-команду compute_team_colors — она
остаётся только как разовый бэкафилл для команд, существовавших ДО того,
как появилось поле primary_color (миграция 0004), и как fallback на
случай, если Celery недоступен.

Проверка `if instance.primary_color: return` — намеренная защита от
дублирования и от бесконечного цикла: сама задача сохраняет посчитанный
цвет через team.save(update_fields=['primary_color']), это тоже вызывает
post_save, но primary_color на этот раз уже не пустой, поэтому сигнал
сразу выходит. Побочный эффект: если у существующей команды меняют
логотип, цвет автоматически НЕ пересчитывается (он ведь уже не пустой) —
для этого случая есть admin-экшен "Пересчитать цвет бренда" в
teams/admin.py.
"""
import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from teams.models import Team
from teams.tasks import compute_team_primary_color_task

logger = logging.getLogger(__name__)


@receiver(post_save, sender=Team)
def on_team_saved(sender, instance, created, **kwargs):
    if instance.primary_color:
        return
    if not (instance.logo or instance.logo_url):
        return
    logger.info('teams.signals: постановка расчёта фирменного цвета в очередь для команды %s', instance.id)
    compute_team_primary_color_task.delay(str(instance.id))
