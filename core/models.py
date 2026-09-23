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


# НОВОЕ (2026-09-23, прямая просьба пользователя "какого раздела не хватает,
# чтобы админить без кода" -> "настройки платформы" — пороги/веса/флаги,
# сейчас захардкоженные в коде или в переменных окружения, правятся ТОЛЬКО
# через правку исходников + редеплой). PlatformSetting — key-value рантайм-
# конфиг с типизацией: значение всегда хранится как TEXT (простая схема,
# одна колонка на все типы), но `typed_value()` приводит его к реальному
# типу по `value_type` при чтении — тот же компромисс, что и у
# `dashboard.ManagementCommandRun.args` (JSONField) в соседнем приложении,
# только тут достаточно плоского текста, т.к. значение всегда скаляр
# (одно число/строка/bool), не структура.
#
# Читается ТОЛЬКО через `get_setting()` ниже (кэш, короткий TTL) — прямой
# `PlatformSetting.objects.get(key=...)` в горячих путях (например, при
# каждом голосовании) создал бы лишний SQL-запрос на каждый чих; сама
# модель — источник правды, кэш — только перед реальными БД-запросами.
class PlatformSetting(models.Model):
    """Одна настройка платформы, доступная для правки в дашборде
    (staff/dashboard/settings/) без деплоя. НЕ предназначена для секретов
    (API-ключи, пароли) — те остаются в переменных окружения, эта модель
    только для операционных порогов/флагов, которые staff должен уметь
    крутить сам (см. `key` verbose_name)."""

    TYPE_STRING = "string"
    TYPE_INT = "int"
    TYPE_FLOAT = "float"
    TYPE_BOOL = "bool"
    TYPE_CHOICES = [
        (TYPE_STRING, _("Текст")),
        (TYPE_INT, _("Целое число")),
        (TYPE_FLOAT, _("Дробное число")),
        (TYPE_BOOL, _("Да/Нет")),
    ]

    # Явный BigAutoField, а не UUID BaseModel — эта модель не участвует ни
    # в каких внешних ссылках (никто не хранит FK на конкретную настройку),
    # и не заводится через API, только через staff-форму: короткий
    # автоинкрементный id читабельнее в консоли/логах, тот же принцип, что
    # у StaffActionLog (dashboard/models.py) и AnalyticsEvent (analytics/
    # models.py). Явно (а не полагаясь на DEFAULT_AUTO_FIELD) — в проекте
    # он нигде не задан глобально, `core/apps.py` его тоже не переопределяет
    # (в отличие от dashboard/aggregates/partners и т.д.), значит без
    # явного поля тут был бы обычный 32-битный AutoField пополам с
    # предупреждением Django — не критично для этой модели, но
    # непоследовательно на фоне остального проекта.
    id = models.BigAutoField(primary_key=True)
    key = models.CharField(_("Ключ"), max_length=100, unique=True)
    value = models.TextField(_("Значение"), blank=True)
    value_type = models.CharField(_("Тип"), max_length=10, choices=TYPE_CHOICES, default=TYPE_STRING)
    description = models.TextField(_("Описание"), blank=True, help_text=_("Что это значение делает и на что влияет — показывается прямо в форме редактирования."))
    updated_at = models.DateTimeField(_("Изменено"), auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name=_("Кем изменено"),
    )

    class Meta:
        verbose_name = _("Настройка платформы")
        verbose_name_plural = _("Настройки платформы")
        ordering = ["key"]

    def __str__(self) -> str:
        return f"{self.key} = {self.value!r}"

    def typed_value(self):
        """Приводит хранимую строку к реальному типу по `value_type`.
        Некорректное значение (например, staff вписал "abc" в числовое
        поле руками через Django admin в обход формы дашборда) молча
        падает на 0/0.0/False, а не роняет вызывающий код — настройка
        конфигурации не должна суметь уронить сайт опечаткой."""
        raw = (self.value or "").strip()
        if self.value_type == self.TYPE_BOOL:
            return raw.lower() in ("1", "true", "yes", "on", "да")
        if self.value_type == self.TYPE_INT:
            try:
                return int(raw)
            except ValueError:
                return 0
        if self.value_type == self.TYPE_FLOAT:
            try:
                return float(raw)
            except ValueError:
                return 0.0
        return raw


PLATFORM_SETTING_CACHE_TTL = 60  # секунд — короткий TTL, не "навсегда": правка в
# дашборде должна применяться быстро (staff не должен ждать рестарта
# воркеров/сайта), но и не бить по БД на каждое обращение.


def get_setting(key: str, default=None):
    """Единая точка чтения PlatformSetting — используйте ТОЛЬКО эту функцию
    в бизнес-логике (не прямой ORM-запрос), чтобы получить дешёвое
    кэширование и единообразный fallback на `default`, если ключ ещё не
    заведён в БД (например, на свежей базе до первого захода в раздел
    «Настройки»)."""
    from django.core.cache import cache

    cache_key = f"platform_setting:{key}"
    cached = cache.get(cache_key)
    if cached is not None:
        return default if cached == _MISSING_SENTINEL else cached

    try:
        setting = PlatformSetting.objects.get(key=key)
        value = setting.typed_value()
    except PlatformSetting.DoesNotExist:
        cache.set(cache_key, _MISSING_SENTINEL, PLATFORM_SETTING_CACHE_TTL)
        return default

    cache.set(cache_key, value, PLATFORM_SETTING_CACHE_TTL)
    return value


# Кэшируем и факт ОТСУТСТВИЯ ключа (не только найденное значение) — без
# этого несуществующий ключ бил бы в БД на КАЖДОЕ обращение (cache.get()
# всегда возвращал бы None -> трактовался бы как "не в кэше" -> постоянный
# DB-запрос), что для часто читаемого, но ещё не заведённого ключа было бы
# даже хуже, чем не иметь кэша вообще. None как сам кэшированный маркер
# использовать нельзя — cache.get(key) неотличим от "ключа в кэше нет".
#
# НАЙДЕНО (2026-09-23, живая проверка в браузере после массового подключения
# новых ключей — TypeError: "'<' not supported between instances of 'int'
# and 'object'" на /players/): сентинел был `object()` — сравнение `cached
# != _MISSING_SENTINEL` держится на identity (object.__eq__ по умолчанию
# сравнивает "это тот же самый объект в памяти"). Кэш здесь — Redis
# (CACHES['default'], настройки проекта), значение всегда проходит через
# pickle туда и обратно. При десериализации из Redis получается НОВЫЙ
# `object()` с другим адресом в памяти — он `!=` оригинальному сентинелу,
# хотя означает то же самое "ключа нет". Раньше эта ветка молча возвращала
# СЫРОЙ сентинел вместо default всякий раз, когда значение "ключа нет" уже
# было закэшировано в Redis и прочитано заново (то есть почти всегда, кроме
# самого первого запроса за 60 секунд) — вызывающий код получал объект
# вместо числа/строки. Раньше это не проявлялось заметно, потому что
# большинство читаемых ключей либо уже были заведены в БД (кэш хранил
# настоящее значение, не сентинел), либо использовались в контексте, где
# "мусорное" значение не роняло страницу. Fix — сентинел заменён на строку
# (JSON/pickle-сериализуемую с сохранением РАВЕНСТВА по значению, а не по
# identity) и сравнение через `==` вместо `!=`.
_MISSING_SENTINEL = "__platform_setting_missing__"

