import uuid
from django.db import models
from django.conf import settings
from django.utils.translation import gettext_lazy as _


class BaseModel(models.Model):

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# НОВОЕ (2026-09-22, прямая просьба пользователя после жалобы "Сергий
# Малий" вместо "Сергий Малый" — механическая транслитерация в принципе не
# может быть 100% верной, реальное написание ФИО нужно проверять по
# внешнему источнику, не гадать по буквам). NAME_SOURCE_CHOICES — общий для
# Player/Referee/Coach (players/models.py, referees/models.py,
# coaches/models.py) набор "откуда взялось текущее ФИО", проставляется
# parsers/sportmonks/importers.py::_resolve_cyrillic_name на КАЖДОМ импорте.
# Живёт в core, а не в parsers (где сама логика транслитерации) — чтобы
# избежать циклического импорта: parsers/sportmonks/importers.py и так
# импортирует players.models/referees.models/coaches.models, обратный
# импорт эти три модели -> parsers создал бы цикл.
#
# Смысл каждого значения — см. подробный докстринг _resolve_cyrillic_name:
#   CLEAN_SOURCE          — Sportmonks сам прислал чистую кириллицу
#                            (доверяем, но именно тут раньше ловились
#                            "Эркин"/"Аскхат" — известные ошибки самого
#                            источника, см. PLAYER_NAME_CORRECTIONS).
#   LATIN_FOREIGN         — распознано как не-славянское имя, кириллицу
#                            осознанно не гадаем, оставлена латиница как есть.
#   GUESSED_TRANSLITERATION — НАШ алгоритм (parsers/sportmonks/translit.py)
#                            угадал кириллицу по правилам — именно эта
#                            категория и дала "Малий" вместо "Малый":
#                            приоритетная цель проверки ИИ (см.
#                            parsers/management/commands/verify_names_with_ai.py).
#   AI_VERIFIED           — подтверждено/исправлено через Gemini API +
#                            одобрено staff в дашборде (см. parsers/models.py::
#                            NameVerificationSuggestion/ConfirmedNameCorrection).
#   MANUAL                — правлено вручную в admin, минуя весь этот пайплайн.
NAME_SOURCE_CLEAN_SOURCE = "clean_source"
NAME_SOURCE_LATIN_FOREIGN = "latin_foreign"
NAME_SOURCE_GUESSED_TRANSLITERATION = "guessed_transliteration"
NAME_SOURCE_AI_VERIFIED = "ai_verified"
NAME_SOURCE_MANUAL = "manual"

NAME_SOURCE_CHOICES = [
    (NAME_SOURCE_CLEAN_SOURCE, _("Кириллица от источника (Sportmonks)")),
    (NAME_SOURCE_LATIN_FOREIGN, _("Иностранное имя, оставлено латиницей")),
    (NAME_SOURCE_GUESSED_TRANSLITERATION, _("Угадано нашей транслитерацией")),
    (NAME_SOURCE_AI_VERIFIED, _("Проверено ИИ, подтверждено staff")),
    (NAME_SOURCE_MANUAL, _("Правлено вручную")),
]

