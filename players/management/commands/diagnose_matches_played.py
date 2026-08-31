# players/management/commands/diagnose_matches_played.py
"""
Диагностика "Матчей сыграно" на странице игрока.

Зачем: код на странице игрока (players/views.py::PlayerDetailView и
PlayerListView) считает "матчей сыграно" строго через
MatchLineupPlayer.objects.filter(player=player, ...).count() — то есть
привязано к конкретному игроку, не к команде. Если на сайте это число
у ВСЕХ игроков одной команды совпадает с числом сыгранных матчей самой
команды — это либо (а) прод работает на старом коде, либо (б) в самой
БД в lineups_matchlineupplayer для матчей этой команды почему-то
записан весь ростер, а не реальная заявка на конкретный матч (проблема
на стороне источника KFF или сбой при импорте, не в текущей view-логике).
Эта команда просто печатает факты по конкретной команде, не чинит
ничего — по выводу сразу видно, какой это случай.

    python manage.py diagnose_matches_played --team "Қайрат"
    python manage.py diagnose_matches_played --team-id <uuid>
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from teams.models import Team
from players.models import Player
from lineups.models import MatchLineupPlayer
from matches.models import Match


class Command(BaseCommand):
    help = 'Диагностика: сравнивает "матчей сыграно" у игроков команды с числом матчей самой команды.'

    def add_arguments(self, parser):
        parser.add_argument('--team', type=str, help='Название команды (частичное совпадение).')
        parser.add_argument('--team-id', type=str, help='UUID команды.')

    def handle(self, *args, **options):
        if options.get('team_id'):
            team = Team.objects.filter(id=options['team_id']).first()
        elif options.get('team'):
            team = Team.objects.filter(name__icontains=options['team']).first()
        else:
            raise CommandError('Укажите --team "Название" или --team-id <uuid>.')

        if not team:
            raise CommandError('Команда не найдена.')

        team_matches = Match.objects.filter(
            Q(home_team=team) | Q(away_team=team),
            status='finished',
        ).count()
        self.stdout.write(self.style.SUCCESS(f'Команда: {team.name} (id={team.id})'))
        self.stdout.write(f'Сыграно матчей КОМАНДОЙ (Match, home/away, finished): {team_matches}')
        self.stdout.write('')

        players = Player.objects.filter(team=team, is_active=True).order_by('last_name', 'first_name')
        if not players:
            self.stdout.write(self.style.WARNING('У команды нет активных игроков в БД.'))
            return

        self.stdout.write(f'{"Игрок":40} {"Матчей (MatchLineupPlayer)":28} {"= матчам команды?"}')
        suspicious = 0
        for player in players:
            player_matches = MatchLineupPlayer.objects.filter(
                player=player,
                lineup__match__status='finished',
            ).count()
            same_as_team = (player_matches == team_matches and team_matches > 0)
            if same_as_team:
                suspicious += 1
            flag = '⚠️  ДА — подозрительно' if same_as_team else 'нет'
            self.stdout.write(f'{str(player):40} {player_matches:<28} {flag}')

        self.stdout.write('')
        if suspicious == 0:
            self.stdout.write(self.style.SUCCESS(
                'Ни у одного игрока число совпадающих с командой матчей не найдено — данные в MatchLineupPlayer выглядят корректно (посчитаны по реальному составу на каждый матч).'
            ))
        elif suspicious == players.count():
            self.stdout.write(self.style.ERROR(
                f'У ВСЕХ {suspicious} игроков число матчей равно числу матчей команды — '
                f'похоже, что MatchLineupPlayer в БД для этой команды содержит весь ростер '
                f'на каждый матч, а не реальную заявку. Нужно проверить сырой ответ KFF API '
                f'(/games/{{id}}/lineup) для нескольких матчей этой команды и, если источник '
                f'действительно присылает полный состав вместо протокола — перепарсить составы '
                f'после уточнения фильтрации в parsers/kff/importers.py::import_lineups.'
            ))
        else:
            self.stdout.write(self.style.WARNING(
                f'У {suspicious} из {players.count()} игроков число совпало с матчами команды — '
                f'может быть совпадением (например у команды мало матчей и все игроки реально '
                f'играли все их), а может быть частичной проблемой с данными по конкретным матчам '
                f'— стоит проверить именно этих игроков вручную.'
            ))
