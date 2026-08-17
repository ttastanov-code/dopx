# core/tests.py
"""
is_rate_limited — единственный rate-limiter в проекте (см. docstring
core/utils.py), в этой сессии подключённый ещё к 4 эндпоинтам (регистрация
уже была прикрыта раньше; password-reset, verify-email, toggle_follow,
react_to_event — новые потребители). Ошибка в самой примитиве расползлась
бы сразу на все 5 точек, поэтому логика fixed-window тестируется отдельно,
на уровне core, а не по одному разу в каждом потребителе.

CACHES переопределён на LocMemCache через @override_settings — прод
использует Redis (dopx/settings.py::CACHES), но сама логика is_rate_limited
работает с любым Django cache-backend через одинаковый API (get/set/incr),
так что тест не должен зависеть от того, поднят ли Redis на машине, где
запускается `manage.py test`.
"""
from __future__ import annotations

import time

from django.test import SimpleTestCase, override_settings

from core.utils import is_rate_limited

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-rate-limiter",
    }
}


@override_settings(CACHES=LOCMEM_CACHES)
class IsRateLimitedTests(SimpleTestCase):
    def setUp(self):
        # LocMemCache — общий процесс кэша на все тесты класса (LOCATION
        # одна и та же), без ручной очистки между тестами один тест мог бы
        # унаследовать бакет предыдущего и получить ложный False/True.
        from django.core.cache import cache
        cache.clear()

    def test_first_call_is_not_limited(self):
        self.assertFalse(is_rate_limited("k1", limit=3, window_seconds=60))

    def test_stays_under_limit_within_window(self):
        for _ in range(3):
            self.assertFalse(is_rate_limited("k2", limit=3, window_seconds=60))

    def test_exceeds_limit_on_the_next_call(self):
        for _ in range(3):
            is_rate_limited("k3", limit=3, window_seconds=60)
        # 4-й вызов в том же окне — лимит уже исчерпан.
        self.assertTrue(is_rate_limited("k3", limit=3, window_seconds=60))

    def test_different_keys_have_independent_buckets(self):
        for _ in range(3):
            is_rate_limited("bucket_a", limit=3, window_seconds=60)
        # bucket_a исчерпан, но bucket_b — отдельный ключ, свежий бакет.
        self.assertFalse(is_rate_limited("bucket_b", limit=3, window_seconds=60))

    def test_resets_after_window_expires(self):
        for _ in range(2):
            is_rate_limited("k4", limit=2, window_seconds=1)
        self.assertTrue(is_rate_limited("k4", limit=2, window_seconds=1))
        time.sleep(1.1)
        self.assertFalse(is_rate_limited("k4", limit=2, window_seconds=1), "окно истекло — бакет должен обнулиться")

    def test_limit_of_one_blocks_second_call_immediately(self):
        self.assertFalse(is_rate_limited("k5", limit=1, window_seconds=60))
        self.assertTrue(is_rate_limited("k5", limit=1, window_seconds=60))
