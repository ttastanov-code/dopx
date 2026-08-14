# core/utils.py
"""
Общие утилиты, переиспользуемые в нескольких приложениях.

НОВЫЙ ФАЙЛ — раньше `get_client_ip` была приватным методом ровно одной вьюхи
(`core/views.py::ContactsView.get_client_ip`, реализация видна в
`ContactSubmissionCreateView`-подобных местах) и нигде больше не
переиспользовалась, хотя IP пользователя нужен ещё как минимум в двух
местах антифрод-контура: при регистрации (`users/views.py::RegisterView`) и
при завершении оценки матча (`evaluations/views.py::EvaluateMatchFinalView`).
Вынесено сюда, чтобы не плодить четвёртую копию одной и той же функции.

Также здесь — лёгкий rate-limiter на Django cache-бэкенде (в проекте уже
настроен Redis, см. `dopx/settings.py::CACHES`), без добавления новой
зависимости вроде `django-ratelimit`. Для точечных лимитов (например,
"не больше 5 регистраций с одного IP в час") этого достаточно и он не тянет
за собой отдельный пакет ради одной функции.
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