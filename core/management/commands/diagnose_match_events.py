# core/management/commands/diagnose_match_events.py
"""
manage.py diagnose_match_events "Кайрат" "Тобол" [--year 2026]

ТОЛЬКО ЧТЕНИЕ. Написана 2026-09-21 в ответ на жалобу пользователя: в матче
Кайрат-Тобол на 45' и 81' минуте лента событий показывает "Гол" вообще без
имени забившего (и без ассиста) — не просто "имя не нашлось" (см. фикс
player_display_name чуть раньше в этот же день), а событие целиком без
единого опознавательного поля, при том что на стороннем сайте на этих
минутах вообще ничего не происходит (реальный счёт там идёт 1:2 (44') →
1:3 (63') → 1:4 (80'), без лишних голов).

Печатает ВЕСЬ сырой extra_data для каждого события матча — единственный
способ понять, что именно прислал Sportmonks на этих "призрачных" минутах
(например: это действительно два отдельных объекта events[] с
developer_name="GOAL", но без player_id/player_name вообще — тогда вопрос
"почему Sportmonks их вообще прислал"; или это НАШ баг импорта, который
раздваивает одно событие на два ряда — тогда дело в import_events).
"""
from __future__ import annotations

import difflib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from core.utils import normalize_kz
from events.models import MatchEvent
from matches.models import Match
from teams.models import Team

# Порог нечёткого совпадения — тот же, что уже используется в проекте для
# опечаток в именах (core/management/commands/dedupe_referees_coaches.py,
# --fuzzy), нужен здесь по той же причине: "Тобол" (как ввёл пользователь)
# и "Тобыл" (реальное написание в базе) — не омограф (normalize_kz их не
# считает одинаковыми), а опечатка/альтернативная транслитерация.
FUZZY_THRESHOLD = 0.8


class Command(BaseCommand):
    help = "Печатает сырой extra_data всех событий матча (диагностика, ничего не меняет)"

    def add_arguments(self, parser):
        parser.add_argument("teams", nargs="+", help="Слова из названий команд, например: Кайрат Тобыл")
        parser.add_argument("--year", type=int, default=None, help="Год матча (по умолчанию — любой)")
        parser.add_argument("--limit", type=int, default=5, help="Сколько последних подходящих матчей показать")

    def handle(self, *args, **options):
        words = options["teams"]
        year = options["year"]
        limit = options["limit"]

        team_ids = []
        all_teams = list(Team.objects.only("id", "name"))
        for word in words:
            normalized = normalize_kz(word)
            # Сначала точное вхождение; если ничего — нечёткое совпадение
            # по всей строке имени (ловит "Тобол"/"Тобыл" и подобное).
            exact = [t.id for t in all_teams if normalized in normalize_kz(t.name)]
            if exact:
                team_ids.extend(exact)
                continue
            fuzzy = [
                t for t in all_teams
                if difflib.SequenceMatcher(None, normalized, normalize_kz(t.name)).ratio() >= FUZZY_THRESHOLD
            ]
            if fuzzy:
                self.stdout.write(self.style.WARNING(
                    f"'{word}' не совпало точно ни с одной командой — беру ближайшее по написанию: "
                    + ", ".join(t.name for t in fuzzy)
                ))
                team_ids.extend(t.id for t in fuzzy)

        if not team_ids:
            raise CommandError(f"Ни одна команда не найдена по словам {words!r} (даже нечётким совпадением)")

        # ВАЖНО: сначала завершённые матчи (там реально есть события), иначе
        # -start_time поднимает наверх ещё не сыгранные будущие матчи в
        # календаре и нужный (уже сыгранный, в прошлом) матч не попадает в
        # limit. Среди завершённых — от новых к старым, как и ожидалось.
        qs = Match.objects.filter(
            Q(home_team_id__in=team_ids) | Q(away_team_id__in=team_ids)
        ).select_related("home_team", "away_team").order_by("-status", "-start_time")
        # status='finished' > остальные по алфавиту не гарантированно, поэтому
        # сортируем явно приоритетом, а не полагаемся на -status.
        qs = sorted(qs, key=lambda m: (m.status != "finished", -m.start_time.timestamp()))
        if year:
            qs = [m for m in qs if m.start_time.year == year]

        matches = qs[:limit]
        if not matches:
            raise CommandError("Матчи не найдены")

        for match in matches:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\n{match.home_team.name} {match.home_score}:{match.away_score} {match.away_team.name} "
                f"({match.start_time:%d.%m.%Y}, id={match.id}, sportmonks_id={match.sportmonks_id})"
            ))
            events = MatchEvent.objects.filter(match=match).order_by("minute", "added_time", "id")
            if not events.exists():
                self.stdout.write("  (событий нет)")
                continue
            for event in events:
                blank = not event.player_id and not (event.extra_data or {}).get("player_name")
                marker = self.style.ERROR("  ПУСТОЕ") if blank and event.event_type == "goal" else ""
                self.stdout.write(
                    f"  {event.display_minute}' {event.get_event_type_display()} "
                    f"[{event.team_side}] player_id={event.player_id} "
                    f"sportmonks_id={event.sportmonks_id}{marker}"
                )
                self.stdout.write(f"    extra_data: {json.dumps(event.extra_data, ensure_ascii=False)}")
