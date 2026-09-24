# parsers/management/commands/apply_cyrillic_names.py
"""manage.py apply_cyrillic_names [--include-review] [--apply]

Имена судей/тренеров на кириллицу:
1. ручной словарь name_translations.py (review — только с --include-review);
2. иначе автотранслитерация (translit.py).
Без --apply — dry-run.
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
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения (по умолчанию — только отчёт, dry-run)")

    def handle(self, *args, **options):
        include_review = options["include_review"]
        dry_run = not options["apply"]
        mode = "ПРИМЕНИТЬ" if not dry_run else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        self._apply(Referee, REFEREE_TRANSLATIONS, "судья", include_review, dry_run)
        self._apply(Coach, COACH_TRANSLATIONS, "тренер", include_review, dry_run)

    def _apply(self, model, translations: dict, label: str, include_review: bool, dry_run: bool):
        applied_dict = applied_auto = skipped_already_cyrillic = skipped_review = skipped_dict_already_correct = 0

        for obj in model.objects.filter(sportmonks_id__isnull=False):
            current_full = f"{obj.first_name} {obj.last_name}".strip()
            sm_id = int(obj.sportmonks_id)
            entry = translations.get(sm_id)

            # Запись из словаря применяется всегда, если отличается от текущей.
            # Проверка «уже чистая кириллица» — только для имён не из словаря.
            if entry is not None:
                first_name, last_name, confidence = entry
                if confidence == "review" and not include_review:
                    skipped_review += 1
                    continue
                if first_name == obj.first_name and last_name == obj.last_name:
                    skipped_dict_already_correct += 1
                    continue
                mark = "" if confidence == "high" else " [review]"
                self.stdout.write(f"  {label} (словарь): {current_full!r} -> {first_name} {last_name}{mark}")
                applied_dict += 1
            elif _is_clean_cyrillic(current_full):
                skipped_already_cyrillic += 1
                continue
            elif is_likely_foreign(current_full):
                # Не славянское имя — оставляем латиницу, не транслитерируем.
                self.stdout.write(f"  {label} (не тронут, похоже на не-славянское): {current_full!r}")
                continue
            else:
                # Нет в словаре — автотранслитерация.
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
            f"по словарю уже верно {skipped_dict_already_correct}, "
            f"вне словаря уже кириллица (пропущено) {skipped_already_cyrillic}"
            + (f", review в словаре пропущено (--include-review) {skipped_review}" if skipped_review else "")
        ))
