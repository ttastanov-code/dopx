# core/tests_season_and_cleanup.py
"""Смена сезона (фиксация сборной, туры прошлого сезона, умолчания, архив на странице лиги)
и очистка ботов со всеми следами.
"""
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate
from analytics.models import AnalyticsEvent
from core.utils import synthetic_users_q
from evaluations.models import ContextEvaluation, EvaluationSession, PlayerEvaluation
from leagues.models import League
from matches.models import Match
from players.models import Player
from round_squad.models import RoundBestXI
from round_squad.services import resolve_default_round
from season_squad.models import SeasonBestXI, SeasonBestXISlot
from season_squad.tasks import recompute_all_active_best_xi, season_is_over
from seasons.models import Season
from teams.models import Team, TeamSeason, TeamSeasonStats

User = get_user_model()


class _SeasonBase(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="КПЛ", country="KZ", is_primary=True)
        self.old = Season.objects.create(league=self.league, year="2025", is_active=False)
        self.new = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")

    def match(self, season, voting_open_until, status="finished", **extra):
        return Match.objects.create(
            league=self.league, season=season, home_team=self.home, away_team=self.away, status=status,
            start_time=timezone.now() - timedelta(days=3), voting_open_until=voting_open_until, **extra,
        )


class SeasonTransitionTests(_SeasonBase):
    def test_past_season_finalized_when_voting_closed(self):
        self.match(self.old, timezone.now() - timedelta(days=1))
        self.match(self.new, timezone.now() - timedelta(days=1))
        self.assertTrue(season_is_over(self.old))
        recompute_all_active_best_xi()
        self.assertTrue(SeasonBestXI.objects.get(season=self.old).is_final)
        # Активный сезон остаётся живым.
        self.assertFalse(SeasonBestXI.objects.get(season=self.new).is_final)

    def test_past_season_waits_while_last_voting_open(self):
        self.match(self.old, timezone.now() + timedelta(hours=10))
        self.assertFalse(season_is_over(self.old))
        recompute_all_active_best_xi()
        self.assertFalse(SeasonBestXI.objects.get(season=self.old).is_final)

    def test_default_round_falls_back_to_previous_season(self):
        RoundBestXI.objects.create(season=self.old, tour=26, is_final=True, finalized_at=timezone.now())
        season, tour = resolve_default_round()
        self.assertEqual((season, tour), (self.old, 26))

    def test_inactive_team_kept_in_past_standings(self):
        TeamSeason.objects.create(team=self.home, season=self.old)
        TeamSeason.objects.create(team=self.away, season=self.old)
        Team.objects.filter(pk=self.away.pk).update(is_active=False)
        self.match(self.old, timezone.now() - timedelta(days=1), home_score=1, away_score=0)
        from aggregates.tasks import recalculate_season_standings

        recalculate_season_standings(self.old.id)
        self.assertTrue(TeamSeasonStats.objects.filter(season=self.old, team=self.away).exists())

    def test_league_page_shows_past_season_best_xi(self):
        best_xi = SeasonBestXI.objects.create(season=self.old, is_final=True)
        from django.contrib.contenttypes.models import ContentType

        player = Player.objects.create(first_name="Лучший", last_name="Вратарь", team=self.home)
        SeasonBestXISlot.objects.create(
            best_xi=best_xi, slot_code="GK", content_type=ContentType.objects.get_for_model(Player),
            object_id=str(player.id), occupant_name="Лучший Вратарь", season_score=7.5,
        )
        response = self.client.get(reverse("leagues:detail", args=[self.league.id]), {"season": "2025"})
        self.assertEqual(response.status_code, 200)
        lines = response.context["season_xi_lines"]
        self.assertEqual([line["label"] for line in lines], ["Вратарь"])
        self.assertContains(response, "Лучший Вратарь")


class CleanupBotsTests(_SeasonBase):
    def _vote(self, user, match, value):
        ContextEvaluation.objects.create(user=user, match=match)
        PlayerEvaluation.objects.create(user=user, match=match, player=self.player, contribution=value, risk=3, potential=5)
        EvaluationSession.objects.create(user=user, match=match, status="completed", completed_at=timezone.now())

    def setUp(self):
        super().setUp()
        self.player = Player.objects.create(first_name="Иван", last_name="Игрок", team=self.home)
        self.m = self.match(self.new, timezone.now() - timedelta(hours=1))
        self.bot = User.objects.create_user(username="test_user_bot_0001", email="test_user_bot_0001@test.dopx.local", password="x")
        self.human = User.objects.create_user(username="real", email="real@example.com", password="x")
        self.staff = User.objects.create_user(username="test_user_admin", email="a@test.dopx.local", password="x", is_staff=True)
        for i in range(5):
            u = User.objects.create_user(username=f"h{i}", email=f"h{i}@example.com", password="x")
            self._vote(u, self.m, 6)
        self._vote(self.bot, self.m, 10)
        AnalyticsEvent.objects.create(event_name=AnalyticsEvent._meta.get_field("event_name").choices[0][0], user=self.bot)

    def test_criterion_skips_staff_and_real_users(self):
        names = set(User.objects.filter(synthetic_users_q()).values_list("username", flat=True))
        self.assertEqual(names, {"test_user_bot_0001"})

    def test_dry_run_deletes_nothing(self):
        call_command("cleanup_test_users", stdout=StringIO())
        self.assertTrue(User.objects.filter(pk=self.bot.pk).exists())

    def test_apply_removes_bot_traces_and_recalculates(self):
        from aggregates.tasks import recalculate_player_aggregates

        recalculate_player_aggregates(str(self.m.id))
        self.assertEqual(PlayerMatchAggregate.objects.get(match=self.m).total_votes, 6)

        call_command("cleanup_test_users", "--apply", stdout=StringIO())

        self.assertFalse(User.objects.filter(pk=self.bot.pk).exists())
        self.assertFalse(PlayerEvaluation.objects.filter(user_id=self.bot.pk).exists())
        self.assertFalse(AnalyticsEvent.objects.filter(user_id=self.bot.pk).exists())
        self.assertEqual(AnalyticsEvent.objects.filter(user__isnull=True).count(), 0)
        agg = PlayerMatchAggregate.objects.get(match=self.m)
        self.assertEqual(agg.total_votes, 5)
        self.assertAlmostEqual(agg.avg_contribution, 6.0, places=2)
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())


class RecalcAllTests(CleanupBotsTests):
    def test_recalc_all_removes_stale_aggregates_without_bots(self):
        from aggregates.tasks import recalculate_player_aggregates

        recalculate_player_aggregates(str(self.m.id))
        call_command("cleanup_test_users", "--apply", "--no-recalc", stdout=StringIO())
        self.assertEqual(PlayerMatchAggregate.objects.get(match=self.m).total_votes, 6)  # устаревший
        call_command("cleanup_test_users", "--recalc-all", stdout=StringIO())
        self.assertEqual(PlayerMatchAggregate.objects.get(match=self.m).total_votes, 5)


class ResetCorrectionsTests(TestCase):
    def test_reset_removes_pending_entity_flags_only(self):
        from users.models import SuspiciousActivityFlag

        user = User.objects.create_user(username="real", email="real@example.com", password="x")
        SuspiciousActivityFlag.objects.create(source="player_stats_divergence", status="pending")
        kept_reviewed = SuspiciousActivityFlag.objects.create(source="player_stats_divergence", status="dismissed")
        kept_user = SuspiciousActivityFlag.objects.create(user=user, source="fast_wizard", status="pending")
        call_command("cleanup_test_users", "--reset-corrections", stdout=StringIO())
        self.assertEqual(
            set(SuspiciousActivityFlag.objects.values_list("id", flat=True)), {kept_reviewed.id, kept_user.id}
        )
