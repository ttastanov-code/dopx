# core/utils.py
"""
Общие утилиты, переиспользуемые в нескольких приложениях.

get_client_ip нужен в нескольких местах антифрод-контура: регистрация
(users/views.py::RegisterView), завершение оценки (evaluations/views.py::
EvaluateMatchFinalView), контакты (core/views.py::ContactsView).

is_rate_limited — лёгкий rate-limiter на Django cache (Redis, см.
dopx/settings.py::CACHES), без зависимости от django-ratelimit — для
точечных лимитов вроде "не больше 5 регистраций с одного IP в час" хватает.
"""
from __future__ import annotations

from django.core.cache import cache
from django.http import HttpRequest


def get_client_ip(request: HttpRequest) -> str | None:
    """
    Определяет реальный IP клиента с учётом обратного прокси.

    Берёт первый адрес из `X-Forwarded-For` (стандартный заголовок,
    который выставляет nginx/любой reverse-proxy перед Gunicorn/Uvicorn),
    и только если его нет — падает обратно на `REMOTE_ADDR`.
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
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