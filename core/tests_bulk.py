# core/tests_bulk.py
"""Тихий режим массовых скриптов: сигналы не ставят задачи в Celery."""
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from core.bulk import is_quiet, quiet


class QuietModeTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_nested_restores_state(self):
        self.assertFalse(is_quiet())
        with quiet():
            with quiet():
                self.assertTrue(is_quiet())
            self.assertTrue(is_quiet())
        self.assertFalse(is_quiet())

    def test_signals_do_not_enqueue(self):
        from aggregates import signals
        from users.tasks import schedule_progress_recompute

        with mock.patch.object(signals.trigger_aggregate_recalculation, "apply_async") as recalc, \
                mock.patch("users.tasks.recompute_user_progress_task.apply_async") as progress:
            with quiet():
                signals._schedule_recalculation("m1", countdown=30)
                schedule_progress_recompute("u1")
            recalc.assert_not_called()
            progress.assert_not_called()
            signals._schedule_recalculation("m1", countdown=30)
            recalc.assert_called_once()
