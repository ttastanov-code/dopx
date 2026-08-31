# teams/tasks.py
"""
Celery-задачи приложения teams.

compute_team_primary_color_task — асинхронный расчёт фирменного цвета
клуба (Team.primary_color) из логотипа. Запускается АВТОМАТИЧЕСКИ сигналом
post_save (см. teams/signals.py) каждый раз, когда сохраняется команда без
ещё посчитанного цвета — вручную запускать ничего не нужно ни для новых
команд, добавленных через админку, ни для команд, которые создаёт парсер
KFF при импорте матчей (parsers/kff/importers.py тоже просто делает
Team.objects.create(...)/get_or_create(...), сигнал сработает одинаково).

Ручной путь остаётся как страховка на случай смены логотипа существующей
команды (сигнал не пересчитывает уже посчитанный цвет — см. docstring в
signals.py): admin-экшен "🎨 Пересчитать цвет бренда" в teams/admin.py и
`python manage.py compute_team_colors --force`.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3)
def compute_team_primary_color_task(self, team_id: str) -> bool:
    """Считает и сохраняет Team.primary_color/secondary_color для одной команды по id."""
    from teams.models import Team
    from teams.services import extract_team_colors

    try:
        team = Team.objects.get(id=team_id)
    except Team.DoesNotExist:
        logger.warning('compute_team_primary_color_task: команда %s не найдена', team_id)
        return False

    try:
        primary, secondary = extract_team_colors(team)
    except Exception as exc:
        logger.error('compute_team_primary_color_task: ошибка для команды %s: %s', team_id, exc)
        raise self.retry(exc=exc, countdown=60)

    if not primary:
        logger.info('compute_team_primary_color_task: цвет не извлечён для команды %s (нет логотипа/не читается)', team_id)
        return False

    team.primary_color = primary
    team.secondary_color = secondary or ''
    team.save(update_fields=['primary_color', 'secondary_color'])
    logger.info('compute_team_primary_color_task: команда %s -> %s / %s', team_id, primary, secondary)
    return True
