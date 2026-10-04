# core/tests_redact.py
"""Секреты не попадают в логи и Sentry."""
import logging

import requests
from django.test import SimpleTestCase

from core.redact import SecretsFilter, redact, scrub_event


class RedactTests(SimpleTestCase):
    def test_patterns(self):
        url = "https://api.sportmonks.com/v3/football/livescores?api_token=SECRET123abc&locale=ru"
        self.assertNotIn("SECRET123abc", redact(url))
        self.assertIn("locale=ru", redact(url))
        self.assertNotIn("123456:ABCDEFGHIJKLMNOPQRSTUV", redact("https://api.telegram.org/bot123456:ABCDEFGHIJKLMNOPQRSTUV/getMe"))
        self.assertNotIn("sk-ant-api03-abcdefghijk", redact("key sk-ant-api03-abcdefghijk"))
        self.assertNotIn("AIzaSyA1234567890abcdefghijklmnopqrstu", redact("x?key=AIzaSyA1234567890abcdefghijklmnopqrstu"))

    def test_log_filter_and_sentry_event(self):
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "fail %s", ("https://h/x?api_token=TOP",), None)
        try:
            raise requests.ConnectionError("Max retries exceeded with url: /v3?api_token=TOPSECRET")
        except requests.ConnectionError:
            import sys
            record.exc_info = sys.exc_info()
        SecretsFilter().filter(record)
        self.assertNotIn("TOP", record.getMessage().replace("<токен>", ""))
        self.assertNotIn("TOPSECRET", record.exc_text)
        event = {"exception": {"values": [{"value": "url ?api_token=TOPSECRET"}]}, "breadcrumbs": [{"message": "api_token=Z1"}]}
        self.assertNotIn("TOPSECRET", str(scrub_event(event)))
        self.assertNotIn("Z1", str(scrub_event(event)))

    def test_sportmonks_client_errors_have_no_token(self):
        from unittest import mock
        from django.test import override_settings
        from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient

        err = requests.ConnectionError("Max retries exceeded with url: /v3/football/livescores?api_token=TOKENVALUE")
        with override_settings(SPORTMONKS_API_TOKEN="TOKENVALUE", DEV_SAFE_MODE=False), \
                mock.patch("parsers.sportmonks.client.time.sleep"), \
                mock.patch.object(requests.Session, "get", side_effect=err):
            with self.assertRaises(SportmonksAPIError) as ctx:
                SportmonksClient(api_token="TOKENVALUE")._get("/livescores")
        self.assertNotIn("TOKENVALUE", str(ctx.exception))
