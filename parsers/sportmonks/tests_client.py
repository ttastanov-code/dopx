# parsers/sportmonks/tests_client.py
"""
ИСПРАВЛЕНО (2026-09-10, реальный краш в проде): `SportmonksClient.get_player`
(и 6 других одиночных геттеров — get_league/get_standings/get_fixture/
get_team/get_referee/get_coach) индексировали `payload['data']` напрямую —
если Sportmonks ответил HTTP 200, но тело ответа не содержит "data" (не
воспроизведено вживую в этой песочнице — нет сети, см. докстринг ниже — но
РЕАЛЬНО случилось на проде при прогоне `fix_foreign_names --all` на первом
же из 836 игроков), голый `['data']` падал НЕПОЙМАННЫМ KeyError и обрывал
весь batch-прогон, вместо того чтобы поймать SportmonksAPIError на одной
записи и продолжить остальные 835.

Тест ниже мокает HTTP-уровень (requests.Session.get), НЕ настоящую сеть —
подтверждает именно контракт `_get_data()`: отсутствие "data" в теле ответа
даёт SportmonksAPIError (которую вызывающий код умеет ловить), а не
KeyError (которую не ловит никто)."""
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
        """Контрольная проверка — не сломали обычный успешный путь."""
        client = SportmonksClient()
        with patch.object(client.session, "get", return_value=_fake_response(200, {"data": {"id": 1, "name": "Test"}})):
            result = client.get_player(1)
            self.assertEqual(result, {"id": 1, "name": "Test"})
