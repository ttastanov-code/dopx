# parsers/management/commands/list_latin_names.py
"""
Находит записи Referee/Coach/Player, чьё имя сейчас НЕ является чистой
кириллицей (латиница целиком, либо смесь латиницы/кириллицы вроде
"Марин Беланчић" — см. parsers/sportmonks/importers.py::_is_clean_cyrillic
и разбор в чате с пользователем 2026-09-08).

Судьи/тренеры почти всегда будут тут появляться (Sportmonks не переводит их
имена — см. importers.py::get_or_create_referee/get_or_create_coach) —
использовать вместе с parsers/sportmonks/name_translations.py +
apply_cyrillic_names.py: если новый sportmonks_id появился здесь и его нет
в name_translations.py — значит это НОВЫЙ судья/тренер, которого ещё не
переводили, нужно добавить вручную.

Игроки в норме почти все переведены (Sportmonks переводит игроков), но не
все — свежие трансферы и часть иностранных имён (сербские/хорватские с
диакритикой в "name", см. докстринг _resolve_cyrillic_name) могут повиснуть
тут надолго, если сам источник так и не разберётся с переводом — это не
баг, а видимость проблемы для ручной правки в админке.

Использование:
    python manage.py list_latin_names                 # все три типа
    python manage.py list_latin_names --only referees
    python manage.py list_latin_names --only coaches
    python manage.py list_latin_names --only players
"""
import re

from django.core.management.base import BaseCommand

from coaches.models import Coach
from parsers.sportmonks.name_translations import COACH_TRANSLATIONS, REFEREE_TRANSLATIONS
from players.models import Player
from referees.models import Referee

_CLEAN_CYRILLIC_RE = re.compile(
    r"^[А-ЯЁа-яёӘәҒғҚқҢңӨөҰұҮүҺһІіЇїЄєЎў\s\-'`\.]+$"
)


def _is_clean_cyrillic(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and bool(_CLEAN_CYRILLIC_RE.match(text))


class Command(BaseCommand):
    help = "Список Referee/Coach/Player с именем не в чистой кириллице (нужен ручной перевод)"

    def add_arguments(self, parser):
        parser.add_argument("--only", choices=["referees", "coaches", "players"], default=None)

    def handle(self, *args, **options):
        only = options.get("only")

        if only in (None, "referees"):
            self._report_referees_or_coaches(Referee, REFEREE_TRANSLATIONS, "Судьи")
        if only in (None, "coaches"):
            self._report_referees_or_coaches(Coach, COACH_TRANSLATIONS, "Тренеры")
        if only in (None, "players"):
            self._report_players()

    def _report_referees_or_coaches(self, model, translations: dict, title: str):
        rows = []
        for obj in model.objects.filter(sportmonks_id__isnull=False):
            full = f"{obj.first_name} {obj.last_name}".strip()
            if _is_clean_cyrillic(full):
                continue
            sm_id = int(obj.sportmonks_id)
            in_dict = sm_id in translations
            rows.append((obj, full, in_dict))

        self.stdout.write(f"\n{title}: {len(rows)} с нечистым именем")
        for obj, full, in_dict in rows:
            status = "есть в name_translations.py (примените apply_cyrillic_names)" if in_dict else "НЕТ в name_translations.py — добавьте перевод вручную"
            self.stdout.write(f"  sportmonks_id={obj.sportmonks_id}: {full!r} — {status}")

    def _report_players(self):
        rows = [
            (p, f"{p.first_name} {p.last_name}".strip())
            for p in Player.objects.filter(sportmonks_id__isnull=False)
            if not _is_clean_cyrillic(f"{p.first_name} {p.last_name}".strip())
        ]
        self.stdout.write(f"\nИгроки: {len(rows)} с нечистым именем (возможен пропуск перевода у Sportmonks, поправить вручную в админке)")
        for p, full in rows:
            self.stdout.write(f"  sportmonks_id={p.sportmonks_id}: {full!r} (id={p.id})")
