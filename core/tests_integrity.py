# core/tests_integrity.py
"""Проверки целостности ловят каждое своё нарушение; итог виден алертам бота."""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from core import integrity
from evaluations.tests import _make_match
from events.models import MatchEvent
from players.models import Player


class IntegrityChecksTests(TestCase):
    def setUp(self):
        cache.clear()
        self.match = _make_match(voting_open_until=timezone.now() - timedelta(hours=1))
        self.match.home_score, self.match.away_score = 1, 0
        self.match.save()

    def test_empty_feed_with_score_is_flagged(self):
        self.assertEqual(integrity.check_score_vs_events().count, 1)
        MatchEvent.objects.create(match=self.match, minute=10, event_type="goal", team_side="home")
        self.assertEqual(integrity.check_score_vs_events().count, 0)

    def test_duplicate_goal_is_flagged(self):
        for minute in (10, 11):
            MatchEvent.objects.create(match=self.match, minute=minute, event_type="goal", team_side="home")
        self.assertEqual(integrity.check_score_vs_events().count, 1)

    def test_standings_mismatch(self):
        from teams.models import TeamSeasonStats

        self.match.season.is_active = True
        self.match.season.save(update_fields=["is_active"])
        TeamSeasonStats.objects.create(team=self.match.home_team, season=self.match.season, played=1, wins=0,
                                       draws=1, losses=0, goals_scored=1, goals_conceded=0, goal_diff=1, points=1)
        finding = integrity.check_standings()
        self.assertEqual(finding.count, 1)
        self.assertIn("wins 0≠1", finding.samples[0])

    def test_aggregate_votes_mismatch(self):
        from aggregates.models import PlayerMatchAggregate

        player = Player.objects.create(first_name="И", last_name="П", team=self.match.home_team)
        PlayerMatchAggregate.objects.create(player=player, match=self.match, performance_score=7, total_votes=12)
        self.assertEqual(integrity.check_aggregate_votes().count, 1)

    def test_user_level_mismatch(self):
        from users.models import User, UserXP

        user = User.objects.create_user(username="u", email="u@ex.com", password="x")
        UserXP.objects.update_or_create(user=user, defaults={"total_xp": 0, "level": 7})
        self.assertEqual(integrity.check_user_levels().count, 1)

    def test_report_feeds_bot_alert(self):
        from adminbot.alerts import check_integrity

        self.assertIsNone(check_integrity())
        report = integrity.run_and_store()
        self.assertGreaterEqual(report["errors"], 1)
        problem = check_integrity()
        self.assertIn("Голы в ленте не сходятся со счётом", problem.detail)
