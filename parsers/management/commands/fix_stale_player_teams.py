# parsers/management/commands/fix_stale_player_teams.py
"""
manage.py fix_stale_player_teams [--apply]

Разовая коррекция уже накопленных "залипших" Player.team (жалоба
пользователя со скриншотом, 2026-09-09: "Виктор Васин" всё ещё в текущем
составе "Кайрат" на странице команды, хотя последний реальный матч за
Кайрат сыграл в сезоне 2024, в 2025/2026 не появлялся вообще).

КОРЕНЬ ПРОБЛЕМЫ (см. правку в parsers/sportmonks/importers.py::
get_or_create_player, тот же коммит): раньше team/number/position
обновлялись КАЖДЫМ импортированным матчем без учёта хронологии — при
бэкафилле нескольких сезонов НЕ по порядку (Sportmonks не гарантирует
порядок в /leagues/393?include=seasons, см. sync_sportmonks_season.py)
фикстура старого сезона, обработанная ПОСЛЕ новых, отматывала team игрока
назад к старому клубу. Эта команда чинит УЖЕ испорченные записи; сам
импортёр с этого коммита защищён от повторного появления той же порчи
(new last_match_at-гейт) — но исторические данные, накопленные ДО фикса,
сами себя не починят, нужен разовый прогон.

ЛОГИКА: для каждого игрока с sportmonks_id ищем его САМУЮ СВЕЖУЮ запись в
MatchLineupPlayer (по match.start_time, среди ЛЮБЫХ статусов матча — сама
лайнап-запись означает, что фикстура реально была импортирована с
составом) — это единственный источник правды о том, за какую команду игрок
играл последним, полностью независимый от порядка импорта (см. докстринг
teams/views.py::TeamDetailView про тот же источник для прошлых сезонов).
Если team в этой записи отличается от текущего Player.team, или
last_match_at не проставлен/расходится — обновляем team, number
(shirt_number), position и last_match_at по этой самой свежей записи.

Игроков БЕЗ единой записи в MatchLineupPlayer (например, добавленных
вручную в админке, или ещё не сыгравших ни матча новичков) команда не
трогает — сравнивать не с чем, трогать их team было бы гаданием.

ИСПРАВЛЕНО (2026-09-10, пользователь прогнал --apply и "нихуя не
изменилось" — "Виктор Васин" остался в составе): ДВЕ отдельные ошибки
нашлись разом.

  1. Раньше queryset был `Player.objects.exclude(sportmonks_id__isnull=True)
     .exclude(sportmonks_id="")` — то есть игроки БЕЗ sportmonks_id
     (легаси-записи ещё с KFF-эпохи, так и не сопоставленные
     reconcile_sportmonks'ом с Sportmonks ID) молча пропускались ЦЕЛИКОМ,
     даже не попадая в счётчик "проверено". Именно такие записи — самые
     вероятные кандидаты на "залипание": raз они никогда не были связаны с
     Sportmonks ID, их НИКОГДА не трогал ни один автосинк вообще, с самого
     cutover'а. Фильтр по sportmonks_id снят — команда теперь смотрит
     ВСЕХ игроков, у кого есть история в MatchLineupPlayer, независимо от
     того, привязаны ли они к Sportmonks.

  2. Если team в MatchLineupPlayer УЖЕ И ТАК совпадает с Player.team (игрок
     реально в последний раз играл именно за эту команду — он просто
     давно не играет ВООБЩЕ, не перешёл в другой клуб), эта команда ничего
     не "чинит" в team, только проставляет last_match_at — а
     teams/views.py::TeamDetailView ДО этой правки last_match_at вообще не
     смотрел, состав по-прежнему решался через голый is_active=True без
     учёта давности. Тот факт, что "команда ничего не изменила" для
     Васина, мог означать именно это: team и так был верным, чинить было
     нечего — баг был не в Player.team, а в отсутствии проверки давности
     на стороне view. См. правку в teams/views.py той же датой — теперь
     "живой ростер" требует last_match_at не старше ~15 месяцев (или
     NULL — новичок, ещё не дебютировавший), не просто is_active=True
     навсегда. last_match_at, проставленный ЭТОЙ командой, — необходимая
     предпосылка для той проверки: без него у всех старых записей
     last_match_at=NULL, что тоже давало бы "год давности" эффект новичка
     — прогнать --apply нужно ОБЯЗАТЕЛЬНО, прежде чем полагаться на
     фильтр в TeamDetailView.

Использование:
    python manage.py fix_stale_player_teams              # только отчёт (dry-run)
    python manage.py fix_stale_player_teams --apply       # применить изменения
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from lineups.models import MatchLineupPlayer
from players.models import Player


class Command(BaseCommand):
    help = "Чинит Player.team/number/position/last_match_at по самой свежей записи в MatchLineupPlayer (разовая коррекция бэкафилла не по хронологии + первичное проставление last_match_at всем игрокам)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения (по умолчанию — только отчёт)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        mode = "ПРИМЕНИТЬ" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        # БЕЗ фильтра по sportmonks_id (см. докстринг, пункт 1) — легаси-
        # игроки без привязки к Sportmonks ID тоже проверяются.
        players = Player.objects.all()
        checked = fixed = no_lineup_data = already_correct = 0

        for player in players.iterator():
            checked += 1
            latest = (
                MatchLineupPlayer.objects
                .filter(player=player, lineup__match__start_time__isnull=False)
                .select_related("lineup__team", "lineup__match")
                .order_by("-lineup__match__start_time")
                .first()
            )
            if latest is None:
                no_lineup_data += 1
                continue

            latest_team = latest.lineup.team
            latest_at = latest.lineup.match.start_time
            latest_number = latest.shirt_number
            latest_position = latest.position

            needs_fix = (
                player.team_id != latest_team.id
                or player.last_match_at is None
                or player.last_match_at < latest_at
            )
            if not needs_fix:
                already_correct += 1
                continue

            self.stdout.write(
                f"  {player.full_name} (id={player.id}): "
                f"team {player.team} -> {latest_team}, "
                f"last_match_at {player.last_match_at} -> {latest_at} "
                f"(матч {latest.lineup.match_id})"
            )
            fixed += 1
            if apply_changes:
                player.team = latest_team
                player.last_match_at = latest_at
                if latest_number is not None:
                    player.number = latest_number
                if latest_position:
                    player.position = latest_position
                player.save(update_fields=["team", "last_match_at", "number", "position", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"Проверено {checked}: исправлено {fixed}, уже корректно {already_correct}, "
            f"без данных в MatchLineupPlayer (не тронуты) {no_lineup_data}"
        ))
