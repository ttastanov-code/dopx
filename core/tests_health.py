# core/tests_health.py
"""/healthz/ и журнал деплоев в дашборде."""
import json

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse


class HealthzTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(APP_VERSION="1.0.0", APP_COMMIT="abc1234")
    def test_degraded_without_heartbeat_then_ok(self):
        from core.tasks import celery_heartbeat

        data = self.client.get(reverse("core:healthz")).json()
        self.assertEqual((data["status"], data["version"], data["commit"], data["db"]), ("degraded", "1.0.0", "abc1234", True))
        celery_heartbeat()
        response = self.client.get(reverse("core:healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertIn("no-cache", response["Cache-Control"])


class DeployHistoryTests(TestCase):
    def test_reads_newest_first(self):
        import tempfile
        from pathlib import Path

        from dashboard.infra_services import deploy_history

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "deploy").mkdir()
            rows = [
                {"started_at": "2026-09-28T10:00:00Z", "status": "success", "sha": "a1", "previous_sha": "a0"},
                {"started_at": "2026-09-28T11:00:00Z", "status": "rolled_back", "sha": "b2", "previous_sha": "a1"},
            ]
            (Path(tmp) / "deploy" / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nbroken\n")
            with override_settings(LOGS_DIR=Path(tmp)):
                history = deploy_history()
        self.assertEqual([h["sha"] for h in history], ["b2", "a1"])
        self.assertEqual(history[0]["status_label"], "Откат")
