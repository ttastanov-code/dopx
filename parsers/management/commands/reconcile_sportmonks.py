# parsers/management/commands/reconcile_sportmonks.py
"""
manage.py reconcile_sportmonks --entity {teams,referees,coaches,players,all} [--apply] [--fuzzy]

Фаза 2 миграции на Sportmonks (docs/sportmonks-migration-plan.md). Цель —
проставить sportmonks_id на УЖЕ СУЩЕСТВУЮЩИЕ записи Team/Referee/Coach/Player,
которые годами копились через импорт из KFF, чтобы дальнейший импорт из
Sportmonks (фаза 3) писал историю ратингов в ТЕ ЖЕ строки, а не плодил
дубликаты рядом со старыми.

Тот же принцип осторожности, что и в core/management/commands/
dedupe_referees_coaches.py, который эта команда сознательно копирует:

1. Без --apply — только отчёт (dry-run), ничего не пишет.
2. Точное совпадение по normalize_kz(имя) — единственное, что пишется
   под --apply. Для команд сопоставление дополнительно ограничено
   контекстом (их всего 16, полный список печатается всегда, даже без
   --fuzzy, чтобы можно было свериться глазами перед --apply).
3. --fuzzy — печатает похожие, но не идентичные пары (порог 0.82, тот же,
   что в dedupe_referees_coaches.py) — ТОЛЬКО отчёт, никогда не пишется
   автоматически. Опечатка неотличима от two разных людей по строке.
4. Игроков намеренно матчим В ГРАНИЦАХ команды (не по всей базе разом) —
   пары "Александр Иванов" в разных клубах не должны склеиваться только
   по совпадению имени. Матчинг игроков имеет смысл запускать после того,
   как команды уже сверены (--entity teams --apply), иначе для команд без
   sportmonks_id игроков сопоставлять не с чем.

Ничего не удаляет и не создаёт новых записей — только проставляет
sportmonks_id на уже существующие. Если для sportmonks-сущности не нашлось
локальной пары (например, судья, который никогда раньше не судил матчи в
нашей истории KFF) — это НЕ ошибка, такая запись будет создана позже самим
импортёром (фаза 3) при первом реальном матче с этим судьёй.
"""
from __future__ import annotations

import difflib
from collections import defaultdict

from django.core.management.base import BaseCommand

from core.utils import normalize_kz
from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient

FUZZY_THRESHOLD = 0.82


class Command(BaseCommand):
    help = "Сопоставляет существующие Team/Referee/Coach/Player с их id в Sportmonks (фаза 2 миграции)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--entity",
            choices=["teams", "referees", "coaches", "players", "all"],
            default="all",
        )
        parser.add_argument("--apply", action="store_true", help="Реально записать sportmonks_id на точные совпадения.")
        parser.add_argument("--fuzzy", action="store_true", help="Показать похожие, но не идентичные пары (только отчёт).")
        parser.add_argument(
            "--season-id",
            type=int,
            default=None,
            help="Sportmonks season id (по умолчанию — текущий сезон SPORTMONKS_LEAGUE_ID).",
        )

    def handle(self, *args, **options):
        self.client = SportmonksClient()
        self.apply_changes = options["apply"]
        self.show_fuzzy = options["fuzzy"]
        mode = "ПРИМЕНИТЬ" if self.apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        season_id = options["season_id"] or self._get_current_season_id()
        self.stdout.write(f"Сезон Sportmonks: {season_id}\n")

        entity = options["entity"]
        if entity in ("teams", "all"):
            self._reconcile_teams(season_id)
        if entity in ("referees", "all"):
            self._reconcile_referees(season_id)
        if entity in ("coaches", "all"):
            self._reconcile_coaches(season_id)
        if entity in ("players", "all"):
            self._reconcile_players(season_id)

    def _get_current_season_id(self) -> int:
        league = self.client.get_league(include="currentSeason")
        season = league.get("currentseason") or {}
        if not season.get("id"):
            raise SystemExit("Не удалось определить текущий сезон — передайте --season-id явно.")
        return season["id"]

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    def _reconcile_teams(self, season_id: int):
        from teams.models import Team

        self.stdout.write(self.style.MIGRATE_HEADING("=== Команды ==="))
        sm_teams = self.client.get_season_teams(season_id)
        local_teams = list(Team.objects.all())

        matches, unmatched_sm, unmatched_local = self._match(
            sm_items=[(t["id"], t["name"]) for t in sm_teams],
            local_items=[(t.id, f"{t.name}") for t in local_teams],
        )

        local_by_id = {t.id: t for t in local_teams}
        for sm_id, sm_name, local_id, local_name in matches:
            self.stdout.write(f'  "{sm_name}" (sm_id={sm_id})  ==  "{local_name}" (id={local_id})')
            if self.apply_changes:
                local_by_id[local_id].sportmonks_id = str(sm_id)
                local_by_id[local_id].save(update_fields=["sportmonks_id"])

        self._report_unmatched("Sportmonks-команды без пары в базе", unmatched_sm)
        self._report_unmatched("Команды в базе без пары в Sportmonks", unmatched_local)

        if self.show_fuzzy:
            self._fuzzy_report(
                [(t["id"], t["name"]) for t in sm_teams],
                [(t.id, t.name) for t in local_teams],
                "Команды",
            )

    # ------------------------------------------------------------------
    # Судьи и тренеры — собираем распределённый по матчам сезона список
    # ------------------------------------------------------------------

    def _collect_officials_from_season(self, season_id: int) -> tuple[dict, dict]:
        """Один проход по всем матчам сезона (включая ещё не сыгранные —
        у них просто не будет referees/coaches в ответе, не ошибка) —
        собирает уникальных судей и тренеров, встретившихся хотя бы раз.
        Дешевле, чем отдельный запрос на каждый матч: это ровно тот же
        принцип "один bulk-вызов вместо цикла", что и в client.py."""
        league = self.client.get_league(include="currentSeason")
        season = league.get("currentseason") or {}
        date_from = (season.get("starting_at") or "")[:10]
        date_to = (season.get("ending_at") or "")[:10]

        fixtures = self.client.get_fixtures_between(
            date_from, date_to, include="referees.referee;coaches",
        )

        referees: dict[int, str] = {}
        coaches: dict[int, str] = {}
        for fx in fixtures:
            for ref_row in fx.get("referees") or []:
                if ref_row.get("type_id") == 6 and ref_row.get("referee"):  # только главный судья
                    r = ref_row["referee"]
                    referees[r["id"]] = r.get("name") or r.get("display_name")
            for c in fx.get("coaches") or []:
                coaches[c["id"]] = c.get("name") or c.get("display_name")

        return referees, coaches

    def _reconcile_referees(self, season_id: int):
        from referees.models import Referee

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Судьи ==="))
        sm_referees, _ = self._collect_officials_from_season(season_id)
        local = list(Referee.objects.all())

        matches, unmatched_sm, unmatched_local = self._match(
            sm_items=list(sm_referees.items()),
            local_items=[(r.id, r.full_name) for r in local],
        )

        local_by_id = {r.id: r for r in local}
        for sm_id, sm_name, local_id, local_name in matches:
            self.stdout.write(f'  "{sm_name}" (sm_id={sm_id})  ==  "{local_name}" (id={local_id})')
            if self.apply_changes:
                local_by_id[local_id].sportmonks_id = str(sm_id)
                local_by_id[local_id].save(update_fields=["sportmonks_id"])

        self._report_unmatched(
            "Sportmonks-судьи без пары в базе (создадутся при первом матче в фазе 3)", unmatched_sm,
        )
        self._report_unmatched("Судьи в базе без пары в Sportmonks", unmatched_local)

        if self.show_fuzzy:
            self._fuzzy_report(list(sm_referees.items()), [(r.id, r.full_name) for r in local], "Судьи")

    def _reconcile_coaches(self, season_id: int):
        from coaches.models import Coach

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Тренеры ==="))
        _, sm_coaches = self._collect_officials_from_season(season_id)
        local = list(Coach.objects.all())

        matches, unmatched_sm, unmatched_local = self._match(
            sm_items=list(sm_coaches.items()),
            local_items=[(c.id, c.full_name) for c in local],
        )

        local_by_id = {c.id: c for c in local}
        for sm_id, sm_name, local_id, local_name in matches:
            self.stdout.write(f'  "{sm_name}" (sm_id={sm_id})  ==  "{local_name}" (id={local_id})')
            if self.apply_changes:
                local_by_id[local_id].sportmonks_id = str(sm_id)
                local_by_id[local_id].save(update_fields=["sportmonks_id"])

        self._report_unmatched(
            "Sportmonks-тренеры без пары в базе (создадутся при первом матче в фазе 3)", unmatched_sm,
        )
        self._report_unmatched("Тренеры в базе без пары в Sportmonks", unmatched_local)

        if self.show_fuzzy:
            self._fuzzy_report(list(sm_coaches.items()), [(c.id, c.full_name) for c in local], "Тренеры")

    # ------------------------------------------------------------------
    # Игроки — строго в границах команды, команды должны быть уже сверены
    # ------------------------------------------------------------------

    def _reconcile_players(self, season_id: int):
        from players.models import Player
        from teams.models import Team

        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Игроки (по командам) ==="))
        teams_with_sm_id = Team.objects.exclude(sportmonks_id__isnull=True).exclude(sportmonks_id="")
        if not teams_with_sm_id.exists():
            self.stdout.write(self.style.ERROR(
                "Ни у одной команды нет sportmonks_id — сначала прогоните "
                "--entity teams --apply, иначе сопоставлять игроков не с чем."
            ))
            return

        total_matched = total_unmatched_sm = total_unmatched_local = 0

        for team in teams_with_sm_id:
            try:
                squad = self.client.get_squad(season_id, int(team.sportmonks_id), include="player")
            except SportmonksAPIError as exc:
                self.stdout.write(self.style.ERROR(f'  {team.name}: ошибка запроса состава — {exc}'))
                continue

            sm_players = [
                (row["player"]["id"], row["player"].get("name") or row["player"].get("display_name"))
                for row in squad if row.get("player")
            ]
            local_players = list(Player.objects.filter(team=team))

            matches, unmatched_sm, unmatched_local = self._match(
                sm_items=sm_players,
                local_items=[(p.id, p.full_name) for p in local_players],
            )

            self.stdout.write(f"  {team.name}: {len(matches)} совпало, "
                               f"{len(unmatched_sm)} новых у Sportmonks, {len(unmatched_local)} без пары в базе")

            local_by_id = {p.id: p for p in local_players}
            for sm_id, sm_name, local_id, local_name in matches:
                if self.apply_changes:
                    local_by_id[local_id].sportmonks_id = str(sm_id)
                    local_by_id[local_id].save(update_fields=["sportmonks_id"])

            if self.show_fuzzy:
                self._fuzzy_report(sm_players, [(p.id, p.full_name) for p in local_players], f"Игроки {team.name}")

            total_matched += len(matches)
            total_unmatched_sm += len(unmatched_sm)
            total_unmatched_local += len(unmatched_local)

        self.stdout.write(self.style.SUCCESS(
            f"\nИтого: совпало {total_matched}, новых у Sportmonks {total_unmatched_sm}, "
            f"без пары в базе {total_unmatched_local}"
        ))

    # ------------------------------------------------------------------
    # Общая логика сопоставления (используется всеми сущностями выше)
    # ------------------------------------------------------------------

    @staticmethod
    def _match(sm_items: list[tuple], local_items: list[tuple]):
        """sm_items/local_items — списки (id, name). Возвращает
        (matches, unmatched_sm, unmatched_local), matches — список
        (sm_id, sm_name, local_id, local_name). Точное совпадение —
        ровно как в dedupe_referees_coaches.py: normalize_kz(name)
        совпадает буквально, без фаззи (фаззи — отдельно, только отчёт)."""
        local_by_key = defaultdict(list)
        for local_id, name in local_items:
            local_by_key[normalize_kz(name)].append((local_id, name))

        matches = []
        unmatched_sm = []
        used_local_ids = set()

        for sm_id, sm_name in sm_items:
            key = normalize_kz(sm_name or "")
            candidates = [c for c in local_by_key.get(key, []) if c[0] not in used_local_ids]
            if candidates:
                local_id, local_name = candidates[0]
                matches.append((sm_id, sm_name, local_id, local_name))
                used_local_ids.add(local_id)
            else:
                unmatched_sm.append((sm_id, sm_name))

        unmatched_local = [(lid, name) for lid, name in local_items if lid not in used_local_ids]
        return matches, unmatched_sm, unmatched_local

    def _fuzzy_report(self, sm_items: list[tuple], local_items: list[tuple], label: str):
        found = False
        for sm_id, sm_name in sm_items:
            sm_key = normalize_kz(sm_name or "")
            for local_id, local_name in local_items:
                local_key = normalize_kz(local_name or "")
                if sm_key == local_key:
                    continue  # точное совпадение — уже обработано выше
                ratio = difflib.SequenceMatcher(None, sm_key, local_key).ratio()
                if ratio >= FUZZY_THRESHOLD:
                    found = True
                    self.stdout.write(
                        f'    [{label}] {ratio:.2f}  Sportmonks "{sm_name}" (id={sm_id})'
                        f'  <->  база "{local_name}" (id={local_id})'
                    )
        if not found:
            self.stdout.write(f"    [{label}] похожих пар не найдено.")

    def _report_unmatched(self, title: str, items: list[tuple]):
        if not items:
            return
        self.stdout.write(self.style.WARNING(f"  {title} ({len(items)}):"))
        for item_id, name in items:
            self.stdout.write(f"    - {name} (id={item_id})")
