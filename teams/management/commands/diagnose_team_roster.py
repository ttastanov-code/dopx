# teams/management/commands/diagnose_team_roster.py
"""
manage.py diagnose_team_roster <название или id команды>

ТОЛЬКО ЧТЕНИЕ, ничего не меняет. Создан 2026-09-10 по прямому запросу
пользователя ("с хуя ли ничего не исправляется?" после того, как
`fix_stale_player_teams --apply` отчитался "0 исправлено, 836 уже
корректно") — это ожидаемый и ПРАВИЛЬНЫЙ результат, если last_match_at
уже был выставлен раньше (например, обычным ходом импорта — get_or_create_
player в parsers/sportmonks/importers.py проставляет last_match_at при
КАЖДОМ импорте матча, не только этой командой), НЕ признак того, что фикс
не сработал. Но проверить это на словах нельзя — нужно посмотреть на
реальные данные.

Печатает РОВНО ТУ ЖЕ логику, что teams/views.py::TeamDetailView использует
для "текущего состава" (current_roster_ids/played_this_season_ids), но с
разбивкой по каждому игроку и явной причиной "включён"/"исключён" — чтобы
можно было увидеть напрямую в базе, а не гадать, сработал фикс или нет,
и не зависеть от того, перезапущен ли gunicorn/celery с новым кодом
(команда всегда читает АКТУАЛЬНЫЙ код на диске в момент запуска).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from lineups.models import MatchLineupPlayer
from players.models import Player
from teams.models import Team
from teams.views import ROSTER_STALE_THRESHOLD


class Command(BaseCommand):
    help = "Диагностика (read-only): почему конкретный игрок есть/нет в 'текущем составе' команды на её странице"

    def add_arguments(self, parser):
        parser.add_argument("team", help="Название команды (частичное совпадение) или её id")

    def handle(self, *args, **options):
        team_query = options["team"]
        team = Team.objects.filter(id=team_query).first() if _looks_like_uuid(team_query) else None
        if team is None:
            team = Team.objects.filter(name__icontains=team_query).first()
        if team is None:
            raise CommandError(f"Команда не найдена: {team_query!r}")

        self.stdout.write(self.style.WARNING(f"Команда: {team.name} (id={team.id})"))

        now = timezone.now()
        cutoff = now - ROSTER_STALE_THRESHOLD
        self.stdout.write(f"ROSTER_STALE_THRESHOLD = {ROSTER_STALE_THRESHOLD.days} дней, cutoff = {cutoff:%Y-%m-%d}")

        from seasons.models import Season
        current_season = Season.get_primary_active()

        played_this_season_ids = set()
        if current_season:
            played_this_season_ids = set(
                Player.objects.filter(
                    matchlineupplayer__lineup__team=team,
                    matchlineupplayer__lineup__match__season=current_season,
                ).values_list("id", flat=True)
            )

        all_team_players = Player.objects.filter(team=team).order_by("last_name", "first_name")
        included, excluded = [], []

        for player in all_team_players:
            reasons = []
            in_current_roster = False

            if not player.is_active:
                reasons.append("is_active=False")
            else:
                if player.last_match_at is None:
                    in_current_roster = True
                    reasons.append("last_match_at=NULL (считается новичком, ещё не дебютировавшим)")
                elif player.last_match_at >= cutoff:
                    in_current_roster = True
                    reasons.append(f"last_match_at={player.last_match_at:%Y-%m-%d} не старше порога")
                else:
                    reasons.append(f"last_match_at={player.last_match_at:%Y-%m-%d} СТАРШЕ порога ({cutoff:%Y-%m-%d})")

            played_this_season = player.id in played_this_season_ids
            if played_this_season:
                in_current_roster = True
                reasons.append("играл за эту команду в текущем сезоне")

            line = f"  {player.full_name} (is_active={player.is_active}, last_match_at={player.last_match_at}): {'; '.join(reasons)}"
            if in_current_roster:
                included.append(line)
            else:
                excluded.append(line)

        self.stdout.write(self.style.SUCCESS(f"\nВКЛЮЧЕНЫ в текущий состав ({len(included)}):"))
        for line in included:
            self.stdout.write(line)

        self.stdout.write(self.style.NOTICE(f"\nИСКЛЮЧЕНЫ ({len(excluded)}):"))
        for line in excluded:
            self.stdout.write(line)

        # Последняя реальная запись MatchLineupPlayer на игрока, у которого
        # is_active=True, но он всё равно исключён "по возрасту" — чтобы
        # видно было, откуда взялась дата last_match_at, не поверив ей на слово.
        stale_active = [
            p for p in all_team_players
            if p.is_active and p.last_match_at is not None and p.last_match_at < cutoff
            and p.id not in played_this_season_ids
        ]
        if stale_active:
            self.stdout.write(self.style.WARNING(
                f"\n{len(stale_active)} активных игроков с устаревшим last_match_at "
                f"(is_active=True не сброшен, но из состава исключены фильтром по давности):"
            ))
            for p in stale_active:
                latest = (
                    MatchLineupPlayer.objects.filter(player=p)
                    .select_related("lineup__team", "lineup__match")
                    .order_by("-lineup__match__start_time")
                    .first()
                )
                latest_desc = (
                    f"{latest.lineup.match.start_time:%Y-%m-%d} за {latest.lineup.team}"
                    if latest else "нет записей в MatchLineupPlayer вообще"
                )
                self.stdout.write(f"  {p.full_name}: последний реальный матч — {latest_desc}")


def _looks_like_uuid(value: str) -> bool:
    import uuid
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError):
        return False
