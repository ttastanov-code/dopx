# parsers/management/commands/apply_cyrillic_names.py
"""
Приводит имена судей/тренеров к кириллице. Два источника, в порядке
приоритета:

1. parsers/sportmonks/name_translations.py — вручную выверенный список (146
   человек за 3 сезона по состоянию на 2026-09-08), в том числе с явной
   пометкой "review" для по-настоящему неоднозначных иностранных фамилий
   (см. докстринг файла).
2. Автоматический транслитератор (parsers/sportmonks/translit.py) — для
   ВСЕГО остального (новых судей/тренеров, которых нет в словаре выше, и
   которых становится всё больше при бэкафилле остальных сезонов). Это то
   же самое, что теперь автоматически включено в get_or_create_referee/
   get_or_create_coach при СОЗДАНИИ новой записи (importers.py) — эта
   команда закрывает пробел ТОЛЬКО для записей, созданных ДО того, как
   транслитератор туда подключили (см. чат: "надо автоматизировать").

Использование:
    python manage.py apply_cyrillic_names                    # high + авто
    python manage.py apply_cyrillic_names --include-review   # + review-словарь
    python manage.py apply_cyrillic_names --dry-run          # только показать

Ничего не трогает у записей, чьё текущее имя УЖЕ чистая кириллица (ручная
правка staff не перезаписывается).
"""
import re

from django.core.management.base import BaseCommand

from coaches.models import Coach
from parsers.sportmonks.name_translations import COACH_TRANSLATIONS, REFEREE_TRANSLATIONS
from parsers.sportmonks.translit import is_likely_foreign, transliterate_name
from referees.models import Referee

_CLEAN_CYRILLIC_RE = re.compile(
    r"^[А-ЯЁа-яёӘәҒғҚқҢңӨөҰұҮүҺһІіЇїЄєЎў\s\-'`\.]+$"
)


def _is_clean_cyrillic(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and bool(_CLEAN_CYRILLIC_RE.match(text))


class Command(BaseCommand):
    help = "Приводит имена судей/тренеров к кириллице (словарь + авто-транслитератор)"

    def add_arguments(self, parser):
        parser.add_argument("--include-review", action="store_true", help="Применить и словарные записи с уверенностью 'review'")
        parser.add_argument("--dry-run", action="store_true", help="Только показать, что будет изменено, ничего не сохранять")

    def handle(self, *args, **options):
        include_review = options["include_review"]
        dry_run = options["dry_run"]

        self._apply(Referee, REFEREE_TRANSLATIONS, "судья", include_review, dry_run)
        self._apply(Coach, COACH_TRANSLATIONS, "тренер", include_review, dry_run)

    def _apply(self, model, translations: dict, label: str, include_review: bool, dry_run: bool):
        applied_dict = applied_auto = skipped_already_cyrillic = skipped_review = 0

        for obj in model.objects.filter(sportmonks_id__isnull=False):
            current_full = f"{obj.first_name} {obj.last_name}".strip()
            if _is_clean_cyrillic(current_full):
                skipped_already_cyrillic += 1
                continue

            sm_id = int(obj.sportmonks_id)
            entry = translations.get(sm_id)

            if entry is not None:
                first_name, last_name, confidence = entry
                if confidence == "review" and not include_review:
                    skipped_review += 1
                    continue
                mark = "" if confidence == "high" else " [review]"
                self.stdout.write(f"  {label} (словарь): {current_full!r} -> {first_name} {last_name}{mark}")
                applied_dict += 1
            elif is_likely_foreign(current_full):
                # ИСПРАВЛЕНО (2026-09-09, баг найден пользователем —
                # "Слиšковиć"/"Йоãо Антóнио..."): транслитератор не знает
                # диакритику романских/южнославянских языков и раньше всё
                # равно запускался на таких именах, оставляя нераспознанные
                # символы "как есть" — получалась мешанина кириллицы с
                # латиницей, хуже исходной чистой латиницы. Теперь на явно
                # не-славянских именах транслитерация не запускается вообще
                # (тот же принцип, что теперь в importers.py::
                # _resolve_cyrillic_name) — current_full и так уже
                # человекочитаемая латиница, трогать нечего.
                self.stdout.write(f"  {label} (не тронут, похоже на не-славянское): {current_full!r}")
                continue
            else:
                # Не в вручную выверенном словаре — автотранслитератор
                # (закрывает пробел для записей, созданных до того, как он
                # был подключён к get_or_create_referee/coach).
                translit = transliterate_name(current_full)
                first_name, last_name = (translit.split(" ", 1) + [""])[:2]
                self.stdout.write(f"  {label} (авто): {current_full!r} -> {translit}")
                applied_auto += 1

            if not dry_run:
                obj.first_name = first_name
                obj.last_name = last_name
                obj.save(update_fields=["first_name", "last_name", "updated_at"])

        self.stdout.write(self.style.SUCCESS(
            f"{label.capitalize()}и: применено из словаря {applied_dict}, "
            f"применено автотранслитератором {applied_auto}, "
            f"уже кириллица (пропущено) {skipped_already_cyrillic}"
            + (f", review в словаре пропущено (--include-review) {skipped_review}" if skipped_review else "")
        ))
