# teams/management/commands/compute_team_colors.py
"""
Бэкафилл Team.primary_color/secondary_color для всех (или выбранных) команд.

Запускать один раз после миграции 0005_team_secondary_color для команд,
которые существовали ДО появления этих полей — дальше для новых команд
цвета считаются автоматически (см. teams/signals.py). Также полезно для
пересчёта, если сменился логотип у уже существующей команды.

    python manage.py compute_team_colors
    python manage.py compute_team_colors --force   # пересчитать даже уже посчитанные
    python manage.py compute_team_colors --team-id <uuid>
"""
import logging

from django.core.management.base import BaseCommand

from teams.models import Team
from teams.services import extract_team_colors

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Извлекает и сохраняет фирменную палитру команд из их логотипов (Team.primary_color/secondary_color).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='Пересчитать цвета даже для команд, у которых primary_color уже задан.',
        )
        parser.add_argument(
            '--team-id',
            type=str,
            help='Пересчитать только для одной команды (UUID).',
        )

    def handle(self, *args, **options):
        teams = Team.objects.all()
        if options.get('team_id'):
            teams = teams.filter(id=options['team_id'])
        if not options.get('force'):
            teams = teams.filter(primary_color='')

        teams = list(teams)
        if not teams:
            self.stdout.write(self.style.WARNING('Нет команд для обработки (все уже посчитаны — используйте --force).'))
            return

        computed = 0
        skipped = 0
        for team in teams:
            primary, secondary = extract_team_colors(team)
            if primary:
                team.primary_color = primary
                team.secondary_color = secondary or ''
                team.save(update_fields=['primary_color', 'secondary_color'])
                computed += 1
                if secondary:
                    self.stdout.write(f'  {team.name}: {primary} + {secondary}')
                else:
                    self.stdout.write(f'  {team.name}: {primary}')
            else:
                skipped += 1
                self.stdout.write(self.style.WARNING(f'  {team.name}: цвет не извлечён (нет логотипа/не читается)'))

        self.stdout.write(self.style.SUCCESS(
            f'Готово: посчитано {computed}, пропущено {skipped} из {len(teams)} команд.'
        ))
