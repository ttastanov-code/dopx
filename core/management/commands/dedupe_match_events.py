# core/management/commands/dedupe_match_events.py
"""
manage.py dedupe_match_events [--apply] [--match-id ID]

2026-09-21, жалоба пользователя со скриншотом (матч Кайрат 1:4 Тобыл,
13.09.2026): в ленте событий на 45' и 81' минуте — "Гол" вообще без имени,
хотя на 44'/80' в тех же матчах уже есть НАСТОЯЩИЙ гол с полным именем.

КОРНЕВАЯ ПРИЧИНА (подтверждено чтением сырого extra_data через
diagnose_match_events, не гипотеза): оба "пустых" события — это ТОТ ЖЕ
самый sportmonks-id (id события), что и у настоящего гола минутой раньше
(44 и 45 — оба id=157899217; 80 и 81 — оба id=157901398), но с
занулёнными player/player_name/related_player_name. Похоже на то, что
Sportmonks в какой-то момент вернул "пустую"/ещё не обогащённую версию
события ОТДЕЛЬНЫМ снимком (например, живой опрос поймал его на долю
секунды раньше полной enrichment-стадии, минута успела натикать на 1),
а СТАРЫЙ импортёр (см. parsers/sportmonks/importers.py::import_events до
правки 2026-09-21) сопоставлял "то же самое событие" только по (минута,
тип, сторона) — раз минута отличалась (44 vs 45), это считалось НОВЫМ
событием, и в базе осело ДВЕ строки на одно и то же событие Sportmonks.

Импортёр уже исправлен (сопоставление теперь идёт по sportmonks_id —
см. MatchEvent.sportmonks_id и докстринг import_events) — новые синки
больше не будут плодить такие дубли. Эта команда — разовая чистка УЖЕ
накопленного мусора: находит группы MatchEvent с одинаковым (match,
sportmonks_id) — то есть более одной строки на одно и то же событие
Sportmonks — оставляет ту, где реально есть игрок (player_id ИЛИ
extra_data.player_name), удаляет остальные (пустые дубли-заглушки).

Как и dedupe_referees_coaches.py — по умолчанию ТОЛЬКО отчёт, --apply
реально удаляет."""
from __future__ import annotations

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from events.models import MatchEvent


def _is_informative(event: MatchEvent) -> bool:
    """Есть ли у события хоть какая-то опознавательная информация об
    игроке — тот же критерий, что и player_display_name (events/models.py),
    только без обращения к БД за связанным Player (event.player_id — FK id,
    его наличие уже говорит о том, что локальный игрок найден)."""
    if event.player_id:
        return True
    return bool((event.extra_data or {}).get("player_name"))


class Command(BaseCommand):
    help = "Находит/удаляет дубли MatchEvent с одинаковым sportmonks_id (см. докстринг файла)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально удалить дубли. Без флага — только отчёт.")
        parser.add_argument("--match-id", type=str, default=None, help="Ограничиться одним матчем (UUID)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        match_id = options["match_id"]

        qs = MatchEvent.objects.exclude(sportmonks_id__isnull=True).select_related("match")
        if match_id:
            qs = qs.filter(match_id=match_id)

        groups: dict[tuple, list[MatchEvent]] = defaultdict(list)
        for event in qs:
            groups[(event.match_id, event.sportmonks_id)].append(event)

        duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not duplicate_groups:
            self.stdout.write(self.style.SUCCESS("Дублей не найдено."))
            return

        mode = "ПРИМЕНИТЬ (удаляю)" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}\n"))

        deleted_total = 0
        unresolved = 0
        for (match_id_, sm_id), events in duplicate_groups.items():
            match = events[0].match
            informative = [e for e in events if _is_informative(e)]
            blank = [e for e in events if not _is_informative(e)]

            self.stdout.write(
                f"Матч {match} (id={match_id_}), sportmonks_id события={sm_id}: "
                f"{len(events)} строк(и) в базе"
            )
            for e in events:
                marker = "с данными" if _is_informative(e) else "ПУСТАЯ"
                self.stdout.write(f"    id={e.id}, минута={e.display_minute}, {marker}")

            if len(informative) == 1 and blank:
                # Однозначный случай (ровно как в жалобе пользователя) —
                # одна содержательная запись, остальные — пустые заглушки.
                to_delete = blank
                self.stdout.write(self.style.SUCCESS(
                    f"    -> оставляю id={informative[0].id} (минута {informative[0].display_minute}), "
                    f"удаляю {len(to_delete)} пустую(ые)"
                ))
                if apply_changes:
                    for e in to_delete:
                        e.delete()
                deleted_total += len(to_delete)
            else:
                # Неоднозначно (ноль или несколько содержательных записей) —
                # не рискуем удалять автоматически, нужны глаза человека.
                unresolved += 1
                self.stdout.write(self.style.ERROR(
                    "    -> НЕОДНОЗНАЧНО (не 1 содержательная запись из группы) — пропущено, разберитесь вручную"
                ))
            self.stdout.write("")

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"Итого: {'удалено' if apply_changes else 'к удалению'} {deleted_total} дубликатов, "
            f"неоднозначных групп (пропущено): {unresolved}"
        ))
