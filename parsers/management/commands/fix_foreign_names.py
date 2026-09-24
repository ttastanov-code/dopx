# parsers/management/commands/fix_foreign_names.py
"""manage.py fix_foreign_names [--all] [--apply]

Перезапрашивает имена игроков у Sportmonks и прогоняет через резолвер.
Без --all — только имена со смесью кириллицы и латиницы/диакритики;
--all — все игроки с sportmonks_id (запрос на игрока).
Судей/тренеров не трогает — для них apply_cyrillic_names.
Без --apply — dry-run.
"""
import re

from django.core.management.base import BaseCommand

from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient
from parsers.sportmonks.importers import _resolve_cyrillic_name
from parsers.sportmonks.translit import is_likely_foreign
from players.models import Player

_CYRILLIC_RE = re.compile(r"[А-ЯЁа-яёӘәҒғҚқҢңӨөҰұҮүҺһІіЇїЄєЎў]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _is_broken(text: str) -> bool:
    """Смесь кириллицы с латиницей (включая диакритику)."""
    text = text or ""
    has_cyrillic = bool(_CYRILLIC_RE.search(text))
    has_ascii_latin = bool(_LATIN_RE.search(text))
    has_foreign_signal = is_likely_foreign(text)
    return has_cyrillic and (has_ascii_latin or has_foreign_signal)


class Command(BaseCommand):
    help = "Чинит имена Player, испорченные багом транслитерации (смесь кириллицы/латиницы), либо (--all) переспрашивает Sportmonks и пересверяет ВСЕХ игроков с текущим резолвером. Для Referee/Coach используйте apply_cyrillic_names --include-review."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения (по умолчанию — только отчёт, dry-run)")
        parser.add_argument(
            "--all", action="store_true",
            help="Обойти ВСЕХ игроков с sportmonks_id (не только визуально испорченных смесью алфавитов) — "
                 "систематическая сверка с источником, найдёт и 'чисто кириллические, но неверные' случаи "
                 "вроде 'Эркин Тапалов' (см. докстринг модуля). Один запрос к Sportmonks на игрока — дороже, "
                 "но разово.",
        )

    def handle(self, *args, **options):
        dry_run = not options["apply"]
        check_all = options["all"]
        mode = "ПРИМЕНИТЬ" if not dry_run else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))
        client = SportmonksClient()

        self.stdout.write(self.style.WARNING(
            "Судьи/тренеры этой командой больше не трогаются — используйте "
            "`python manage.py apply_cyrillic_names --include-review` "
            "(вручную выверенный словарь на всех 103 судьях/43 тренерах "
            "КПЛ, включая Слишковича и Гонсалвиша)."
        ))

        if check_all:
            candidates = list(Player.objects.filter(sportmonks_id__isnull=False))
            self.stdout.write(f"Режим --all: сверяю ВСЕХ {len(candidates)} игроков с sportmonks_id против свежих данных Sportmonks (это займёт время)...")
        else:
            candidates = [
                obj for obj in Player.objects.filter(sportmonks_id__isnull=False)
                if _is_broken(f"{obj.first_name} {obj.last_name}".strip())
            ]
        if not candidates:
            self.stdout.write("Игроки: испорченных смешанным именем не найдено (для полной сверки с источником используйте --all)")
            return

        fixed = failed = unchanged = 0
        for obj in candidates:
            old_full = f"{obj.first_name} {obj.last_name}".strip()
            try:
                entity_data = client.get_player(int(obj.sportmonks_id))
            except SportmonksAPIError as exc:
                self.stderr.write(f"  игрок sportmonks_id={obj.sportmonks_id} ({old_full!r}): не удалось запросить у Sportmonks — {exc}")
                failed += 1
                continue

            new_first, new_last = _resolve_cyrillic_name(entity_data, "игрока")
            new_full = f"{new_first} {new_last}".strip()
            if not new_full:
                self.stderr.write(f"  игрок sportmonks_id={obj.sportmonks_id} ({old_full!r}): Sportmonks не вернул имя, пропущен")
                failed += 1
                continue

            if new_first == obj.first_name and new_last == obj.last_name:
                unchanged += 1
                continue

            self.stdout.write(f"  игрок: {old_full!r} -> {new_full!r}")
            if not dry_run:
                obj.first_name = new_first
                obj.last_name = new_last
                obj.save(update_fields=["first_name", "last_name", "updated_at"])
            fixed += 1

        suffix = " (--dry-run, ничего не сохранено)" if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"Игроки: проверено {len(candidates)}, расходится с источником {fixed}, "
            f"уже верно {unchanged}, ошибок запроса {failed}{suffix}"
        ))
