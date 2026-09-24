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
