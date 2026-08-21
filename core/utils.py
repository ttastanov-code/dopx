# core/utils.py
"""
Общие утилиты, переиспользуемые в нескольких приложениях.

get_client_ip нужен в нескольких местах антифрод-контура: регистрация
(users/views.py::RegisterView), завершение оценки (evaluations/views.py::
EvaluateMatchFinalView), контакты (core/views.py::ContactsView).

is_rate_limited — лёгкий rate-limiter на Django cache (Redis, см.
dopx/settings.py::CACHES), без зависимости от django-ratelimit — для
точечных лимитов вроде "не больше 5 регистраций с одного IP в час" хватает.

normalize_kz/KAZAKH_LOOKALIKE_MAP — раньше жили ТОЛЬКО в
dashboard/parser_tools.py (поиск матча в staff-панели по названию команд),
но тот же баг ("Кайрат" по-русски не находит "Қайрат") оказался и в
поиске на страницах teams/players/coaches/referees — вынесено сюда как
общую утилиту, чтобы не дублировать таблицу транслитерации в 5 местах.
"""
from __future__ import annotations

from django.core.cache import cache
from django.http import HttpRequest

# 9 казахских букв (Ә Ғ Қ Ң Ө Ұ Ү Һ І) — отдельные Unicode-символы, не
# просто похожие на русские: "Қ" (U+049A) и "К" (U+041A) для icontains
# полностью не связаны. У большинства пользователей нет казахской
# раскладки — схлопываем оба варианта буквы в один канонический символ
# перед сравнением строк.
KAZAKH_LOOKALIKE_MAP = str.maketrans({
    "ә": "а", "Ә": "А",
    "ғ": "г", "Ғ": "Г",
    "қ": "к", "Қ": "К",
    "ң": "н", "Ң": "Н",
    "ө": "о", "Ө": "О",
    "ұ": "у", "Ұ": "У",
    "ү": "у", "Ү": "У",
    "һ": "х", "Һ": "Х",
    "і": "и", "І": "И",
})


def normalize_kz(text: str) -> str:
    """Казахская буква и её русский "омограф" после этой функции дают
    ОДИНАКОВУЮ строку — сравнение перестаёт зависеть от того, какой
    раскладкой набирали запрос/название."""
    return (text or "").translate(KAZAKH_LOOKALIKE_MAP).lower()


def get_client_ip(request: HttpRequest) -> str | None:
    """
    Определяет реальный IP клиента с учётом обратного прокси.

    БАГ, КОТОРЫЙ ТУТ БЫЛ: брался ПЕРВЫЙ адрес из `X-Forwarded-For` — эта
    часть заголовка целиком под контролем клиента (curl -H "X-Forwarded-For:
    1.2.3.4" ...), значит IP-based rate limit (регистрация, антифрод,
    is_rate_limited) обходился одной строкой заголовка.

    Правильно: `X-Forwarded-For` — это ЦЕПОЧКА "клиент, прокси1, прокси2,
    ...", которая РАСТЁТ по мере прохождения запроса через каждый реальный
    прокси (nginx с `proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for;` ДОПИСЫВАЕТ IP в конец, не заменяет). Если
    перед Gunicorn стоит РОВНО settings.TRUSTED_PROXY_COUNT доверенных
    прокси, то последние TRUSTED_PROXY_COUNT записей — это IP самих
    прокси (их нельзя подделать снаружи, их дописал наш же nginx), а
    элемент ПЕРЕД ними — это и есть настоящий IP клиента. Всё, что левее —
    контролируется атакующим и не заслуживает доверия.

    ВАЖНО: это не замена сетевой защиты — если Gunicorn доступен извне
    напрямую (в обход nginx), у атакующего в заголовке будет всего один
    (поддельный) элемент, который и окажется "на нужной позиции". Прямой
    доступ к Gunicorn должен быть закрыт файрволом/security group — см.
    docs/BACKLOG.md.
    """
    from django.conf import settings

    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        chain = [ip.strip() for ip in x_forwarded_for.split(",") if ip.strip()]
        trusted_proxy_count = getattr(settings, "TRUSTED_PROXY_COUNT", 1)
        if chain:
            # Индекс с конца: -1 - N доверенных прокси. Если в цепочке
            # меньше звеньев, чем ожидается доверенных прокси (заголовок
            # подделан/укорочен), безопаснее откатиться на самый левый
            # известный элемент, чем на REMOTE_ADDR самого nginx.
            client_index = len(chain) - 1 - trusted_proxy_count
            return chain[client_index] if client_index >= 0 else chain[0]
    return request.META.get("REMOTE_ADDR")


def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """
    Простой rate-limiter по алгоритму fixed window на Django cache.

    :param key: уникальный ключ бакета, например `f"register:{ip}"`.
    :param limit: сколько попыток разрешено за окно.
    :param window_seconds: длительность окна в секундах.
    :return: True, если лимит уже исчерпан (запрос СЛЕДУЕТ отклонить).

    Не претендует на идеальную точность скользящего окна (fixed window
    подвержен "двойному всплеску" на границе окна) — для защиты от
    примитивного бот-фарминга регистраций этого достаточно; для более
    строгих гарантий стоит переходить на `django-ratelimit`/токен-бакет.
    """
    cache_key = f"ratelimit:{key}"
    current = cache.get(cache_key)
    if current is None:
        cache.set(cache_key, 1, timeout=window_seconds)
        return False
    if current >= limit:
        return True
    try:
        cache.incr(cache_key)
    except ValueError:
        # Ключ протух между get() и incr() — считаем это новым окном.
        cache.set(cache_key, 1, timeout=window_seconds)
    return False