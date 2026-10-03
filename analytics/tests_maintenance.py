# analytics/tests_maintenance.py
"""Ночная чистка аналитики: итоги дня остаются, старые сырые события удаляются."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from analytics.models import AnalyticsDailyStat, AnalyticsEvent, EventName
from analytics.tasks import analytics_maintenance


@override_settings(ANALYTICS_RAW_DAYS=120, ANALYTICS_KEEP_DAYS=730)
class MaintenanceTests(TestCase):
    def _event(self, name, days_ago, anon=None):
        e = AnalyticsEvent.objects.create(event_name=name, anonymous_id=anon)
        AnalyticsEvent.objects.filter(pk=e.pk).update(created_at=timezone.now() - timedelta(days=days_ago))

    def test_rollup_then_purge(self):
        import uuid

        a, b = uuid.uuid4(), uuid.uuid4()
        for anon in (a, a, b):
            self._event(EventName.PAGE_VIEW, 200, anon)
        self._event(EventName.USER_REGISTERED, 200)
        self._event(EventName.USER_REGISTERED, 800)
        self._event(EventName.PAGE_VIEW, 1)

        result = analytics_maintenance()

        # Старые просмотры удалены, регистрация 200-дневной давности осталась, 800-дневная — нет.
        self.assertEqual(result["deleted"], 4)
        self.assertEqual(set(AnalyticsEvent.objects.values_list("event_name", flat=True)),
                         {EventName.PAGE_VIEW, EventName.USER_REGISTERED})
        old = AnalyticsDailyStat.objects.get(event_name=EventName.PAGE_VIEW,
                                             date=timezone.localdate() - timedelta(days=200))
        self.assertEqual((old.events, old.visitors), (3, 2))
        self.assertTrue(AnalyticsDailyStat.objects.filter(date=timezone.localdate() - timedelta(days=1)).exists())
        # Повторный запуск ничего не ломает.
        analytics_maintenance()
        self.assertEqual(AnalyticsDailyStat.objects.get(pk=old.pk).events, 3)


    @override_settings(ANALYTICS_RAW_DAYS=120, ANALYTICS_KEEP_DAYS=0)
    def test_keep_forever_by_default_and_other_tables_untouched(self):
        from evaluations.models import EvaluationSession

        self._event(EventName.USER_REGISTERED, 3000)
        self._event(EventName.PAGE_VIEW, 3000)
        sessions = EvaluationSession.objects.count()
        analytics_maintenance()
        self.assertEqual(list(AnalyticsEvent.objects.values_list("event_name", flat=True)), [EventName.USER_REGISTERED])
        self.assertEqual(EvaluationSession.objects.count(), sessions)
