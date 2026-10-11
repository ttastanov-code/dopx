# parsers/sportmonks/tests_client.py
"""Тесты SportmonksClient: ответ без "data" -> SportmonksAPIError, а не KeyError."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient


def _fake_response(status_code: int, json_body: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.text = str(json_body)
    return resp


@override_settings(SPORTMONKS_API_TOKEN="test-token", SPORTMONKS_BASE_URL="https://api.sportmonks.com/v3/football")
class GetDataMissingKeyTests(TestCase):
    def test_response_without_data_key_raises_api_error_not_key_error(self):
        client = SportmonksClient()
        with patch.object(client.session, "get", return_value=_fake_response(200, {"message": "no data"})):
            with self.assertRaises(SportmonksAPIError):
                client.get_player(9999999)

    def test_error_message_includes_response_body_for_debugging(self):
        client = SportmonksClient()
        with patch.object(client.session, "get", return_value=_fake_response(200, {"message": "subscription required"})):
            with self.assertRaises(SportmonksAPIError) as ctx:
                client.get_player(9999999)
            self.assertIn("subscription required", str(ctx.exception))

    def test_normal_response_with_data_key_still_works(self):
        """Обычный ответ работает."""
        client = SportmonksClient()
        with patch.object(client.session, "get", return_value=_fake_response(200, {"data": {"id": 1, "name": "Test"}})):
            result = client.get_player(1)
            self.assertEqual(result, {"id": 1, "name": "Test"})


class NetworkErrorTests(TestCase):
    """Нет ответа вовсе — SportmonksNetworkError (временно); ответ с ошибкой — обычная SportmonksAPIError."""

    @override_settings(SPORTMONKS_API_TOKEN="t")
    def test_dns_failure_is_network_error_with_given_retries(self):
        import requests
        from parsers.sportmonks.client import SportmonksClient, SportmonksNetworkError

        client = SportmonksClient(retries=2, timeout=8)
        with patch.object(client.session, "get", side_effect=requests.ConnectionError("Failed to resolve")) as get, \
                patch("parsers.sportmonks.client.time.sleep"):
            with self.assertRaises(SportmonksNetworkError):
                client.get_fixture(1)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["timeout"], 8)

    @override_settings(SPORTMONKS_API_TOKEN="t")
    def test_http_500_is_api_error_not_network(self):
        from parsers.sportmonks.client import SportmonksAPIError, SportmonksClient, SportmonksNetworkError

        client = SportmonksClient(retries=1)
        resp = MagicMock(status_code=500, headers={})
        with patch.object(client.session, "get", return_value=resp), patch("parsers.sportmonks.client.time.sleep"):
            with self.assertRaises(SportmonksAPIError) as ctx:
                client.get_fixture(1)
        self.assertNotIsInstance(ctx.exception, SportmonksNetworkError)


class LivePollTimeLimitTests(TestCase):
    def test_soft_time_limit_is_swallowed_and_lock_released(self):
        from celery.exceptions import SoftTimeLimitExceeded
        from django.core.cache import cache
        from parsers.sportmonks import tasks

        with patch.object(tasks, "_sportmonks_update_live_impl", side_effect=SoftTimeLimitExceeded()):
            self.assertIsNone(tasks.sportmonks_update_live())
        self.assertIsNone(cache.get(tasks.LIVE_POLL_OVERLAP_LOCK_KEY))
