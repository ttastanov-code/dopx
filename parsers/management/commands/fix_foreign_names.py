# parsers/management/commands/fix_foreign_names.py
"""
Разовый ремонт записей Player, испорченных багом транслитерации
не-славянских/латинских имён (найдено пользователем 2026-09-09 на живых
данных).

ПЕРЕСМОТРЕНО (2026-09-09, ВАЖНО — первая версия этой команды сама сломала
данные): первая версия трогала ТАКЖЕ Referee/Coach, запрашивала свежее имя
у Sportmonks и пересчитывала его алгоритмом — и переписала УЖЕ ВЕРНОЕ
"Александр Кержаков" (взятое из вручную выверенного словаря
parsers/sportmonks/name_translations.py) обратно в сломанное "Алеxандр
Кержаков", потому что (а) не смотрела в словарь вообще и (б) в алгоритме
был ЕЩЁ ОДИН баг — "x" не транслитерировался (см. фикс в translit.py).
Заодно её детектор "испорченности" (искал ТОЛЬКО ASCII-латиницу A-Za-z
рядом с кириллицей) не ловил порчу диакритикой (š/ć/ã/ó/ç — это НЕ ASCII,
regex их не видел), поэтому "Владимир Слиšковиć" вообще не попал в
обработку и остался как был.

Для Referee/Coach это в принципе НЕПРАВИЛЬНЫЙ инструмент: Sportmonks не
переводит имена судей/тренеров вообще (см. get_or_create_referee/
get_or_create_coach в importers.py), а обратная транслитерация
принципиально неоднозначна (см. докстринг name_translations.py) — для этих
двух категорий уже есть авторитетный, вручную выверенный на ВСЕХ 103
судьях и 43 тренерах КПЛ трёх сезонов источник: parsers/sportmonks/
name_translations.py + `python manage.py apply_cyrillic_names
--include-review` (дай именно её для Слишковича/Гонсалвиша — обе записи
там уже есть, confidence="review"). Эта команда для Referee/Coach БОЛЬШЕ
НИЧЕГО НЕ ДЕЛАЕТ, только подсказывает использовать apply_cyrillic_names.

Для Player — Sportmonks ОБЫЧНО переводит имена сам, алгоритм — уже
установленный резервный путь (см. translit.py докстринг), поэтому
живой повторный запрос + пересчёт через (уже исправленный) резолвер
остаётся оправданным здесь.

Использование:
    python manage.py fix_foreign_names                # только игроки
    python manage.py fix_foreign_names --dry-run       # только показать
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
    """Испорченное имя — смесь кириллицы с ЛЮБОЙ латиницей (обычной ASCII
    ИЛИ диакритикой типа š/ć/ã) в одной строке. ИСПРАВЛЕНО: первая версия
    проверяла только ASCII A-Za-z и пропускала диакритику — используем
    is_likely_foreign() (та же функция, что и в резолвере имён) как
    дополнительный сигнал, она уже знает полный набор диакритики."""
    text = text or ""
    has_cyrillic = bool(_CYRILLIC_RE.search(text))
    has_ascii_latin = bool(_LATIN_RE.search(text))
    has_foreign_signal = is_likely_foreign(text)
    return has_cyrillic and (has_ascii_latin or has_foreign_signal)


class Command(BaseCommand):
    help = "Чинит имена Player, испорченные багом транслитерации (смесь кириллицы/латиницы). Для Referee/Coach используйте apply_cyrillic_names --include-review."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Только показать, что будет изменено, ничего не сохранять")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        client = SportmonksClient()

        self.stdout.write(self.style.WARNING(
            "Судьи/тренеры этой командой больше не трогаются — используйте "
            "`python manage.py apply_cyrillic_names --include-review` "
            "(вручную выверенный словарь на всех 103 судьях/43 тренерах "
            "КПЛ, включая Слишковича и Гонсалвиша)."
        ))

        broken = [
            obj for obj in Player.objects.filter(sportmonks_id__isnull=False)
            if _is_broken(f"{obj.first_name} {obj.last_name}".strip())
        ]
        if not broken:
            self.stdout.write("Игроки: испорченных смешанным именем не найдено")
            return

        fixed = failed = 0
        for obj in broken:
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

            self.stdout.write(f"  игрок: {old_full!r} -> {new_full!r}")
            if not dry_run:
                obj.first_name = new_first
                obj.last_name = new_last
                obj.save(update_fields=["first_name", "last_name", "updated_at"])
            fixed += 1

        suffix = " (--dry-run, ничего не сохранено)" if dry_run else ""
        self.stdout.write(self.style.SUCCESS(
            f"Игроки: испорченных найдено {len(broken)}, починено {fixed}, ошибок {failed}{suffix}"
        ))
