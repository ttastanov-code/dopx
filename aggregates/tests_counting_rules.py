# aggregates/tests_counting_rules.py
"""Правила подсчёта: какие голоса идут в рейтинг, лагеря болельщиков, команда игрока
в матче, доверие по независимым голосам, снятие поправок, турнирная таблица, номинации.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from aggregates import tasks as agg_tasks
from aggregates.models import PlayerMatchAggregate, PlayerRatingCorrection, RefereeMatchAggregate, TeamMatchAggregate
from aggregates.services import (
    build_allegiance,
    calculate_user_trust_adjustment,
    compute_bias_score,
    countable_evaluations,
)
from core.nominations import get_nominations
from evaluations.models import ContextEvaluation, EvaluationSession, PlayerEvaluation, RefereeEvaluation
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from referees.models import Referee
from seasons.models import Season
from teams.models import Team, TeamSeason, TeamSeasonStats
from users.models import SuspiciousActivityFlag

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="L", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")
        self.player = Player.objects.create(first_name="Иван", last_name="Тест", team=self.home)
        self._n = 0

    def make_match(self, days_ago=0, **extra):
        self._n += 1
        start = timezone.now() - timedelta(days=days_ago, minutes=self._n)
        defaults = dict(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status="finished", start_time=start, end_time=start + timedelta(hours=2),
            voting_open_until=timezone.now() + timedelta(hours=48),
        )
        defaults.update(extra)
        return Match.objects.create(**defaults)

    def make_user(self, name, email=None, **extra):
        return User.objects.create_user(
            username=name, email=email or f"{name}@example.com", password="pass12345", is_verified=True, **extra,
        )

    def complete(self, user, match):
        EvaluationSession.objects.update_or_create(
            user=user, match=match, defaults={"status": "completed", "completed_at": timezone.now()},
        )

    def vote(self, user, match, contribution, player=None, complete=True, supported_team=None):
        ContextEvaluation.objects.update_or_create(
            user=user, match=match, defaults={"watched_type": "partial", "supported_team": supported_team},
        )
        PlayerEvaluation.objects.update_or_create(
            user=user, match=match, player=player or self.player,
            defaults={"contribution": contribution, "risk": 3, "potential": 5},
        )
        if complete:
            self.complete(user, match)


class CountableVotesTests(_Base):
    def _countable_users(self, match):
        qs = countable_evaluations(PlayerEvaluation.objects.filter(match=match), match.id)
        return set(qs.values_list("user__username", flat=True))

    def test_only_completed_sessions_count(self):
        m = self.make_match()
        self.vote(self.make_user("done"), m, 7)
        self.vote(self.make_user("api_only"), m, 10, complete=False)
        self.assertEqual(self._countable_users(m), {"done"})

    def test_banned_user_excluded(self):
        m = self.make_match()
        banned = self.make_user("banned")
        self.vote(banned, m, 10)
        self.vote(self.make_user("ok"), m, 6)
        User.objects.filter(pk=banned.pk).update(is_active=False)
        self.assertEqual(self._countable_users(m), {"ok"})

    def test_confirmed_flag_excludes_user_in_that_match_only(self):
        m1, m2 = self.make_match(1), self.make_match(0)
        cheater = self.make_user("cheater")
        self.vote(cheater, m1, 10)
        self.vote(cheater, m2, 10)
        SuspiciousActivityFlag.objects.create(user=cheater, match=m1, source="ip_cluster", status="confirmed")
        self.assertEqual(self._countable_users(m1), set())
        self.assertEqual(self._countable_users(m2), {"cheater"})

    def test_pending_flag_does_not_exclude(self):
        m = self.make_match()
        u = self.make_user("suspect")
        self.vote(u, m, 8)
        SuspiciousActivityFlag.objects.create(user=u, match=m, source="fast_wizard", status="pending")
        self.assertEqual(self._countable_users(m), {"suspect"})

    @override_settings(COUNT_SYNTHETIC_VOTES=False)
    def test_synthetic_accounts_excluded_when_disabled(self):
        m = self.make_match()
        self.vote(self.make_user("bot", email="bot@test.dopx.local"), m, 10)
        self.vote(self.make_user("human"), m, 6)
        self.assertEqual(self._countable_users(m), {"human"})

    def test_aggregate_removed_when_all_votes_gone(self):
        m = self.make_match()
        u = self.make_user("solo")
        self.vote(u, m, 9)
        agg_tasks.recalculate_player_aggregates(str(m.id))
        self.assertTrue(PlayerMatchAggregate.objects.filter(match=m).exists())
        User.objects.filter(pk=u.pk).update(is_active=False)
        agg_tasks.recalculate_player_aggregates(str(m.id))
        self.assertFalse(PlayerMatchAggregate.objects.filter(match=m).exists())


class AllegianceTests(_Base):
    def test_undeclared_fan_inferred_from_history(self):
        fan = self.make_user("fan")
        for d in (3, 2):
            past = self.make_match(d)
            ContextEvaluation.objects.create(user=fan, match=past, supported_team=self.home)
        m = self.make_match(0)
        ContextEvaluation.objects.create(user=fan, match=m, supported_team=None)
        supported, established = build_allegiance([fan.id], m)
        self.assertEqual(supported[fan.id], self.home.id)
        self.assertNotIn(fan.id, established)

    def test_fresh_neutral_is_not_anchor(self):
        m = self.make_match(0)
        newbie = self.make_user("newbie")
        ContextEvaluation.objects.create(user=newbie, match=m, supported_team=None)
        _supported, established = build_allegiance([newbie.id], m)
        self.assertNotIn(newbie.id, established)

    def test_neutral_with_history_is_anchor(self):
        veteran = self.make_user("veteran")
        for d in (5, 4, 3):
            self.complete(veteran, self.make_match(d))
        m = self.make_match(0)
        ContextEvaluation.objects.create(user=veteran, match=m, supported_team=None)
        _supported, established = build_allegiance([veteran.id], m)
        self.assertIn(veteran.id, established)


class TeamAtMatchTimeTests(_Base):
    def test_segmentation_uses_lineup_team_not_current_club(self):
        m = self.make_match()
        lineup = MatchLineup.objects.create(match=m, team=self.away, side="away")
        MatchLineupPlayer.objects.create(lineup=lineup, player=self.player, is_starting=True)
        # Игрок уже перешёл в «Home», а в этом матче играл за «Away».
        away_fan = self.make_user("awayfan")
        self.vote(away_fan, m, 9, supported_team=self.away)
        agg_tasks.recalculate_player_aggregates(str(m.id))
        agg = PlayerMatchAggregate.objects.get(player=self.player, match=m)
        self.assertEqual(agg.own_fans_avg, 9.0)
        self.assertIsNone(agg.rival_fans_avg)

    def test_bias_score_none_without_history(self):
        m = self.make_match()
        u = self.make_user("fan")
        ContextEvaluation.objects.create(user=u, match=m, supported_team=self.home)
        self.assertIsNone(compute_bias_score(u, m))


class TrustTests(_Base):
    def _others(self, m, values):
        for i, v in enumerate(values):
            self.vote(self.make_user(f"o{i}"), m, v)

    def test_independent_accurate_vote_gains_trust(self):
        m = self.make_match()
        me = self.make_user("me")
        self.vote(me, m, 7)
        PlayerEvaluation.objects.filter(user=me).update(updated_at=timezone.now() - timedelta(hours=1))
        self._others(m, [7, 7, 7])
        self.assertEqual(calculate_user_trust_adjustment(me, m), 0.05)

    def test_vote_after_rating_became_public_gives_nothing(self):
        m = self.make_match()
        self._others(m, [7, 7, 7, 7, 7, 7])
        me = self.make_user("me")
        self.vote(me, m, 7)
        self.assertEqual(calculate_user_trust_adjustment(me, m), 0.0)

    def test_settlement_waits_for_voting_close(self):
        from users.tasks import settle_trust_scores_task

        m = self.make_match()
        me = self.make_user("me")
        self.vote(me, m, 7)
        self.assertEqual(settle_trust_scores_task(), 0)
        Match.objects.filter(pk=m.pk).update(voting_open_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(settle_trust_scores_task(), 1)
        self.assertIsNotNone(EvaluationSession.objects.get(user=me, match=m).trust_settled_at)
        self.assertEqual(settle_trust_scores_task(), 0)


class CorrectionDismissalTests(_Base):
    def test_dismissal_strips_baked_correction(self):
        m = self.make_match()
        self.vote(self.make_user("v"), m, 6)
        agg_tasks.recalculate_player_aggregates(str(m.id), apply_correction=False)
        clean = PlayerMatchAggregate.objects.get(player=self.player, match=m).performance_score
        PlayerRatingCorrection.objects.create(player=self.player, correction=0.3)
        agg_tasks.recalculate_player_aggregates(str(m.id))

        from django.contrib.contenttypes.models import ContentType

        flag = SuspiciousActivityFlag.objects.create(
            content_type=ContentType.objects.get_for_model(Player), object_id=str(self.player.id),
            source="player_stats_divergence",
        )
        agg_tasks.apply_divergence_dismissal([flag])
        agg = PlayerMatchAggregate.objects.get(player=self.player, match=m)
        self.assertAlmostEqual(agg.performance_score, clean, places=2)
        self.assertEqual(agg.rating_correction_applied, 0.0)


class RefereeTests(_Base):
    def test_stale_referee_aggregate_removed_when_referee_changes(self):
        ref1 = Referee.objects.create(first_name="A", last_name="One")
        ref2 = Referee.objects.create(first_name="B", last_name="Two")
        m = self.make_match(referee=ref1)
        u = self.make_user("r")
        ContextEvaluation.objects.create(user=u, match=m)
        RefereeEvaluation.objects.create(user=u, match=m, influence_score=20, decision_quality=8)
        self.complete(u, m)
        agg_tasks.recalculate_referee_aggregates(str(m.id))
        Match.objects.filter(pk=m.pk).update(referee=ref2)
        agg_tasks.recalculate_referee_aggregates(str(m.id))
        self.assertEqual(
            list(RefereeMatchAggregate.objects.filter(match=m).values_list("referee_id", flat=True)), [ref2.id]
        )


class StandingsTests(_Base):
    def test_finished_match_without_score_not_counted(self):
        TeamSeason.objects.create(team=self.home, season=self.season)
        TeamSeason.objects.create(team=self.away, season=self.season)
        self.make_match(home_score=None, away_score=None)
        self.make_match(home_score=2, away_score=1)
        agg_tasks.recalculate_season_standings(self.season.id)
        home = TeamSeasonStats.objects.get(team=self.home, season=self.season)
        away = TeamSeasonStats.objects.get(team=self.away, season=self.season)
        self.assertEqual((home.played, home.wins, home.losses), (1, 1, 0))
        self.assertEqual((away.played, away.wins, away.losses), (1, 0, 1))


class NominationTests(_Base):
    def test_single_high_match_does_not_win_nomination(self):
        m = self.make_match()
        TeamMatchAggregate.objects.create(team=self.home, match=m, avg_effort=10.0, total_votes=50)
        noms = {n["key"] for n in get_nominations()}
        self.assertNotIn("fighting_team", noms)

    def test_enough_matches_and_votes_win(self):
        for d in (3, 2, 1):
            m = self.make_match(d, voting_open_until=timezone.now() - timedelta(hours=1))
            TeamMatchAggregate.objects.create(team=self.home, match=m, avg_effort=8.0, total_votes=6)
        noms = {n["key"]: n for n in get_nominations()}
        self.assertIn("fighting_team", noms)
        self.assertEqual(noms["fighting_team"]["entity_id"], self.home.id)


class PlayerLeaderboardTests(_Base):
    def test_single_low_vote_match_not_on_leaderboard(self):
        from django.urls import reverse

        star = Player.objects.create(first_name="Один", last_name="Голос", team=self.home)
        closed = timezone.now() - timedelta(hours=1)
        PlayerMatchAggregate.objects.create(
            player=star, match=self.make_match(5, voting_open_until=closed), performance_score=10.0, total_votes=1,
        )
        for d in (3, 2, 1):
            PlayerMatchAggregate.objects.create(
                player=self.player, match=self.make_match(d, voting_open_until=closed), performance_score=7.0, total_votes=6,
            )
        response = self.client.get(reverse("users:player_leaderboard"))
        self.assertEqual(response.status_code, 200)
        names = [p.last_name for p in response.context["players"]]
        self.assertEqual(names, ["Тест"])

    def test_league_filter_and_garbage_value(self):
        from django.urls import reverse

        closed = timezone.now() - timedelta(hours=1)
        for d in (3, 2, 1):
            PlayerMatchAggregate.objects.create(
                player=self.player, match=self.make_match(d, voting_open_until=closed), performance_score=7.0, total_votes=6,
            )
        url = reverse("users:player_leaderboard")
        self.assertEqual(len(self.client.get(url, {"league": str(self.league.id)}).context["players"]), 1)
        self.assertEqual(self.client.get(url, {"league": "junk"}).status_code, 200)


class RatingsVisibilityTests(_Base):
    def setUp(self):
        super().setUp()
        self.match = self.make_match()
        PlayerMatchAggregate.objects.create(player=self.player, match=self.match, performance_score=8.0, total_votes=6)

    def _detail(self):
        from django.urls import reverse

        return self.client.get(reverse("matches:detail", args=[self.match.id]))

    def test_hidden_from_anonymous_while_voting_open(self):
        response = self._detail()
        self.assertTrue(response.context["ratings_hidden"])
        self.assertEqual(list(response.context["top_players"]), [])

    def test_visible_to_user_who_completed_evaluation(self):
        me = self.make_user("voted")
        self.complete(me, self.match)
        self.client.force_login(me)
        response = self._detail()
        self.assertFalse(response.context["ratings_hidden"])
        self.assertEqual(len(response.context["top_players"]), 1)

    def test_visible_to_all_after_voting_closed(self):
        Match.objects.filter(pk=self.match.pk).update(voting_open_until=timezone.now() - timedelta(minutes=1))
        response = self._detail()
        self.assertFalse(response.context["ratings_hidden"])

    def test_player_page_skips_open_match(self):
        from django.urls import reverse

        response = self.client.get(reverse("players:detail", args=[self.player.id]))
        self.assertEqual(list(response.context["aggregates"]), [])

    def test_dna_share_card_404_while_open(self):
        from aggregates.models import MatchAggregate
        from django.urls import reverse

        MatchAggregate.objects.create(match=self.match, total_votes=6, drama_index=50)
        response = self.client.get(reverse("core:match_dna_share_card", args=[self.match.id]))
        self.assertEqual(response.status_code, 404)


class BiasProfileWithHistoryTests(_Base):
    def test_profile_computed_from_lineup_teams(self):
        from aggregates.services import compute_bias_profile

        fan = self.make_user("fan")
        rival_player = Player.objects.create(first_name="Чужой", last_name="Игрок", team=self.away)
        for d in (4, 3, 2):
            past = self.make_match(d)
            ContextEvaluation.objects.create(user=fan, match=past, supported_team=self.home)
            PlayerEvaluation.objects.create(user=fan, match=past, player=self.player, contribution=9, risk=3, potential=5)
            PlayerEvaluation.objects.create(user=fan, match=past, player=rival_player, contribution=3, risk=3, potential=5)
            EvaluationSession.objects.create(user=fan, match=past, status="completed")
        current = self.make_match(0)
        ContextEvaluation.objects.create(user=fan, match=current, supported_team=self.home)
        profile = compute_bias_profile(fan, current)
        self.assertEqual(profile["considered"], 3)
        self.assertAlmostEqual(profile["mean_diff"], 6.0)
        self.assertEqual(profile["extreme_ratio"], 1.0)
