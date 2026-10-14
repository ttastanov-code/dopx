# matches/tests_demo_match.py
from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from leagues.models import League
from lineups.models import MatchLineup
from matches.models import Match
from seasons.models import Season
from teams.models import Team


@override_settings(ALLOW_SEED_COMMANDS=True)
class DemoMatchCommandTests(TestCase):
    def setUp(self):
        league = League.objects.create(name="KPL", country="KZ")
        season = Season.objects.create(league=league, year="2026")
        self.home, self.away = Team.objects.create(name="Улытау"), Team.objects.create(name="Окжетпес")
        start = timezone.now() - timedelta(days=5)
        self.match = Match.objects.create(league=league, season=season, home_team=self.home, away_team=self.away,
                                          start_time=start, status="finished", voting_open_until=start + timedelta(hours=48))
        MatchLineup.objects.create(match=self.match, team=self.home, side="home")
        MatchLineup.objects.create(match=self.match, team=self.away, side="away")

    def test_open_then_close(self):
        from events.models import MatchEvent

        MatchEvent.objects.create(match=self.match, minute=10, event_type="goal", team_side="home")
        call_command("demo_match", team="улытау")
        self.match.refresh_from_db()
        self.assertTrue(self.match.is_voting_open())
        self.assertTrue(self.match.manual_override)

        call_command("demo_match", close=True)
        self.match.refresh_from_db()
        self.assertFalse(self.match.is_voting_open())
        self.assertFalse(self.match.manual_override)
