# core/utils.py
"""Общие утилиты: IP клиента, rate-limit, нормализация казахских букв,
проверка тестовых email, статистика для страниц входа.
"""
from __future__ import annotations

from django.core.cache import cache
from django.http import HttpRequest


def get_auth_panel_stats() -> dict:
    """Цифры платформы для панели входа/регистрации — см. core.stats.platform_stats."""
    from core.stats import platform_stats

    return platform_stats()


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


# Почтовые сервисы, где точки в имени ящика не значимы.
DOTLESS_EMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def canonical_email(email: str | None) -> str:
    """Один ящик — одна строка: регистр, «+метка», точки у Gmail. Для поиска дублей аккаунтов."""
    email = (email or "").strip().lower()
    if "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    local = local.split("+", 1)[0]
    if domain in DOTLESS_EMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    return f"{local}@{domain}"


FORM_TIMESTAMP_SALT = "core.form-rendered-at"
# Форма старше — считаем устаревшей (перезагрузить страницу).
FORM_TIMESTAMP_MAX_AGE_SECONDS = 6 * 60 * 60


def sign_form_timestamp() -> str:
    """Подписанное время рендера формы для time-trap (клиент его не подделает)."""
    from django.core import signing

    return signing.TimestampSigner(salt=FORM_TIMESTAMP_SALT).sign("form")


def form_timestamp_is_valid(token: str | None, min_seconds: float) -> bool:
    """Подпись верна, форма не устарела и заполнялась не быстрее min_seconds."""
    from django.core import signing

    if not token:
        return False
    signer = signing.TimestampSigner(salt=FORM_TIMESTAMP_SALT)
    try:
        signer.unsign(token, max_age=FORM_TIMESTAMP_MAX_AGE_SECONDS)
    except signing.BadSignature:
        return False
    try:
        signer.unsign(token, max_age=min_seconds)
    except signing.SignatureExpired:
        return True  # прошло больше min_seconds — человек
    except signing.BadSignature:
        return False
    return False


# Префиксы username сид/нагрузочных команд.
SYNTHETIC_USERNAME_PREFIXES = ("test_user", "loadtest_")
# Домены email тестовых команд вне .dopx.local.
SYNTHETIC_EMAIL_DOMAINS = ("test.dopx.kz", "test.com")


def synthetic_users_q(prefix: str = ""):
    """Q синтетических аккаунтов (боты, нагрузочные, тестовые). prefix — путь до пользователя, напр. "user__"."""
    from django.db.models import Q

    q = Q(**{f"{prefix}email__iendswith": TEST_EMAIL_DOMAIN_SUFFIX}) | Q(**{f"{prefix}email__iendswith": "@dopx.local"})
    for domain in SYNTHETIC_EMAIL_DOMAINS:
        q |= Q(**{f"{prefix}email__iendswith": f"@{domain}"})
    for username_prefix in SYNTHETIC_USERNAME_PREFIXES:
        q |= Q(**{f"{prefix}username__startswith": username_prefix})
    # Сотрудники синтетическими не бывают.
    return q & Q(**{f"{prefix}is_staff": False}) & Q(**{f"{prefix}is_superuser": False})


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