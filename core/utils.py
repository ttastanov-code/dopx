# core/utils.py
"""Общие утилиты: IP клиента, rate-limit, нормализация казахских букв,
проверка тестовых email, статистика для страниц входа.
"""
from __future__ import annotations

from django.core.cache import cache
from django.http import HttpRequest


def get_auth_panel_stats() -> dict:
    """Три реальных числа платформы для панели на страницах входа/регистрации.
    Кэш 10 минут. Импорты моделей внутри функции — модуль грузится рано.
    """
    cache_key = 'auth_panel_stats_v1'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    from evaluations.models import MatchEvaluation
    from matches.models import Match
    from users.models import User

    stats = {
        'total_matches': Match.objects.filter(status='finished').count(),
        'total_evaluations': MatchEvaluation.objects.count(),
        'total_users': User.objects.filter(is_verified=True).count(),
    }
    cache.set(cache_key, stats, 600)
    return stats

# Казахские буквы (Ә Ғ Қ Ң Ө Ұ Ү Һ І) схлопываем в русские аналоги.
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
    """Казахская буква и её русский аналог дают одну и ту же строку."""
    return (text or "").translate(KAZAKH_LOOKALIKE_MAP).lower()


def get_client_ip(request: HttpRequest) -> str | None:
    """Реальный IP клиента за обратным прокси.
    Берём элемент X-Forwarded-For перед последними TRUSTED_PROXY_COUNT записями —
    левее всё подделывается клиентом. Gunicorn не должен быть доступен напрямую.
    """
    from django.conf import settings

    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        chain = [ip.strip() for ip in x_forwarded_for.split(",") if ip.strip()]
        trusted_proxy_count = getattr(settings, "TRUSTED_PROXY_COUNT", 1)
        if chain:
            # Индекс клиента: len(chain) - trusted_proxy_count.
            client_index = len(chain) - trusted_proxy_count
            return chain[client_index] if client_index >= 0 else chain[0]
    return request.META.get("REMOTE_ADDR")


# Все синтетические аккаунты — на поддоменах .dopx.local.
TEST_EMAIL_DOMAIN_SUFFIX = ".dopx.local"


def is_synthetic_test_email(email: str | None) -> bool:
    """True для email тестовых/сид-аккаунтов."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return False
    domain = email.rsplit("@", 1)[-1]
    return domain == "dopx.local" or domain.endswith(TEST_EMAIL_DOMAIN_SUFFIX)


def is_rate_limited(key: str, limit: int, window_seconds: int) -> bool:
    """Rate-limiter fixed window на Django cache.

    :param key: ключ бакета, напр. f"register:{ip}".
    :param limit: попыток за окно.
    :param window_seconds: длина окна.
    :return: True, если лимит исчерпан.

    cache.add() + incr() — атомарно, без гонки.
    """
    cache_key = f"ratelimit:{key}"
    # add() создаёт 0, если ключа нет; incr() атомарно увеличивает.
    cache.add(cache_key, 0, timeout=window_seconds)
    try:
        current = cache.incr(cache_key)
    except ValueError:
        # Ключ истёк между add() и incr() — новое окно.
        cache.add(cache_key, 0, timeout=window_seconds)
        current = cache.incr(cache_key)
    # Первый вызов даёт 1, поэтому сравнение строго «больше».
    return current > limit