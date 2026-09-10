# parsers/management/commands/fix_known_wrong_names.py
"""
manage.py fix_known_wrong_names [--apply]

Разовая коррекция УЖЕ СОХРАНЁННЫХ в базе записей Player/Referee/Coach,
испорченных известными ошибками перевода САМОГО Sportmonks (найдено
пользователем 2026-09-10 — на сайте у четырёх казахских игроков неверная
кириллица: "Эркин" вместо "Еркин" (Тапалов), "Рафаел" вместо "Рафаэль"
(Уразбахтин), "Аскхат" вместо "Асхат" (Тагыберген), "Мукагалы" вместо
"Мукагали" (Пангерей)).

КОРЕНЬ ПРОБЛЕМЫ (см. подробный докстринг parsers/sportmonks/importers.py::
_apply_known_name_corrections и parsers/sportmonks/name_translations.py::
PLAYER_NAME_CORRECTIONS): Sportmonks для казахских игроков сам присылает
готовый, но иногда наивный/неверный кириллический перевод — _resolve_
cyrillic_name принимал ЛЮБОЙ "полностью кириллический" текст как
корректный (валидность букв — не то же самое, что правильность перевода).

ИСПРАВЛЕНО ВТОРОЙ РАЗ (2026-09-10, тот же день — "у нас всё ещё Эркин
Тапалов" ПОСЛЕ первого прогона этой команды с --apply): первая версия этой
команды и PLAYER_NAME_CORRECTIONS в name_translations.py были ДВУМЯ
РАЗНЫМИ словарями — эта команда матчила по УЖЕ ПЕРЕВЕДЁННОЙ (неверной)
кириллице напрямую, а словарь в импортёре — по СЫРОЙ ЛАТИНИЦЕ firstname/
lastname. Для Тапалова и остальных Sportmonks шлёт готовую кириллицу ПРЯМО
в firstname/lastname (не латиницу), поэтому латинская сверка на импорте
никогда не совпадала, а get_or_create_player обновляет имя игрока
ЗАНОВО при КАЖДОМ синке (см. её докстринг) — значит первый же следующий
импорт откатывал то, что эта команда только что исправила напрямую в базе.
Теперь ОБА места (эта команда и _apply_known_name_corrections в importers.py)
используют ОДИН словарь PLAYER_NAME_CORRECTIONS (сам он теперь тоже
ключуется по неверной кириллице, не по латинице) — правка на импорте
больше не может разойтись с разовой коррекцией и откатить её.

ЧЕМ ЭТА КОМАНДА ОТЛИЧАЕТСЯ ОТ apply_cyrillic_names.py: та команда (и
стоящий за ней словарь REFEREE_TRANSLATIONS/COACH_TRANSLATIONS) покрывает
ТОЛЬКО Referee/Coach, ключуется по sportmonks_id и по докстрингу вообще не
касается Player. Эта команда проверяет ВСЕ ТРИ модели (Player/Referee/
Coach — на случай, если то же самое имя/фамилия когда-нибудь встретится у
судьи или тренера) и матчит напрямую по ТЕКУЩЕМУ (уже испорченному)
кириллическому значению поля.

Использование:
    python manage.py fix_known_wrong_names              # только отчёт (dry-run)
    python manage.py fix_known_wrong_names --apply       # применить изменения
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from coaches.models import Coach
from parsers.sportmonks.name_translations import PLAYER_NAME_CORRECTIONS
from players.models import Player
from referees.models import Referee

# Единый источник истины — см. parsers/sportmonks/name_translations.py::
# PLAYER_NAME_CORRECTIONS (ключ — неверная кириллица в нижнем регистре,
# значение — верная). Ключи в этой команде сравниваются case-insensitive
# (.lower()) на случай, если в базе значение отличается регистром от
# канонической записи в словаре.
KNOWN_WRONG_CYRILLIC = PLAYER_NAME_CORRECTIONS


class Command(BaseCommand):
    help = (
        "Чинит Player/Referee/Coach с известными неверными кириллическими именами "
        "от Sportmonks (см. parsers/sportmonks/name_translations.py::PLAYER_NAME_CORRECTIONS)"
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Реально записать изменения (по умолчанию — только отчёт)")

    def handle(self, *args, **options):
        apply_changes = options["apply"]
        mode = "ПРИМЕНИТЬ" if apply_changes else "ТОЛЬКО ОТЧЁТ (dry-run, --apply чтобы применить)"
        self.stdout.write(self.style.WARNING(f"Режим: {mode}"))

        total_fixed = 0
        for model, label in ((Player, "игрок"), (Referee, "судья"), (Coach, "тренер")):
            total_fixed += self._fix_model(model, label, apply_changes)

        suffix = " (--apply чтобы применить)" if not apply_changes else ""
        self.stdout.write(self.style.SUCCESS(
            f"Итого исправлено записей: {total_fixed}{suffix}"
        ))

    def _fix_model(self, model, label: str, apply_changes: bool) -> int:
        fixed = 0
        for obj in model.objects.all():
            update_fields = []
            old_first, old_last = obj.first_name, obj.last_name

            # .lower() — словарь теперь ключуется в нижнем регистре (см.
            # PLAYER_NAME_CORRECTIONS), а в базе значения нормально
            # капитализированы ("Эркин", не "эркин").
            new_first = KNOWN_WRONG_CYRILLIC.get((obj.first_name or "").strip().lower())
            if new_first is not None and new_first != obj.first_name:
                obj.first_name = new_first
                update_fields.append("first_name")

            new_last = KNOWN_WRONG_CYRILLIC.get((obj.last_name or "").strip().lower())
            if new_last is not None and new_last != obj.last_name:
                obj.last_name = new_last
                update_fields.append("last_name")

            if not update_fields:
                continue

            self.stdout.write(
                f"  {label} id={obj.id} (sportmonks_id={obj.sportmonks_id}): "
                f"{old_first!r} {old_last!r} -> {obj.first_name!r} {obj.last_name!r}"
            )
            fixed += 1
            if apply_changes:
                obj.save(update_fields=update_fields + ["updated_at"])

        return fixed
