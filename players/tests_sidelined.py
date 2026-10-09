"""Травмы и дисквалификации: актуальность, причины, импорт."""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from parsers.sportmonks.importers import import_sidelined, sidelined_reason
from players.models import Player, PlayerSidelined
from teams.models import Team


class SidelinedTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Кайрат")
        self.player = Player.objects.create(first_name="Иван", last_name="Иванов", team=self.team, sportmonks_id="77")
        self.today = timezone.now().date()

    def _period(self, **kw):
        return PlayerSidelined(player=self.player, **kw)

    def test_open_ended_injury_goes_stale(self):
        self.assertTrue(self._period(category="injury", start_date=self.today - timedelta(days=30)).is_current)
        self.assertFalse(self._period(category="injury", start_date=self.today - timedelta(days=400)).is_current)
        self.assertFalse(self._period(category="suspended", start_date=self.today - timedelta(days=90)).is_current)
        # С датой окончания в будущем — действует, даже если началось давно.
        self.assertTrue(self._period(category="injury", start_date=self.today - timedelta(days=400),
                                     end_date=self.today + timedelta(days=5)).is_current)

    def test_reason_fixes(self):
        self.assertEqual(sidelined_reason("Аннулирование за жёлтую карточку"), "Перебор жёлтых карточек")
        self.assertEqual(sidelined_reason("Неизвестная травма"), "")
        self.assertEqual(sidelined_reason("Травма стопы"), "Травма стопы")
        self.assertEqual(self._period(category="injury").label, "Травма")

    def test_import_skips_completed_and_stores_type(self):
        start = (self.today - timedelta(days=10)).isoformat()
        import_sidelined(self.team, [
            {"id": 1, "player_id": 77, "category": "injury", "start_date": start, "games_missed": 3,
             "completed": False, "type": {"name": "Травма стопы"}},
            {"id": 2, "player_id": 77, "category": "injury", "start_date": start, "completed": True},
        ])
        rows = list(PlayerSidelined.objects.all())
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].reason, rows[0].games_missed), ("Травма стопы", 3))
