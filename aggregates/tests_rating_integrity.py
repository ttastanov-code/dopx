# aggregates/tests_rating_integrity.py
"""Тесты честности рейтингов и антифрода: средние с учётом голосов, вшитая поправка,
актуализация флага, «Отклонить» в дашборде, оценка по статистике, доминирование,
детекторы тренеров и судей, калибровка весов.
"""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.core.management import call_command
from django.db.models import Avg
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from aggregates.models import (
    CoachMatchAggregate,
    PlayerMatchAggregate,
    PlayerRatingCorrection,
)
from aggregates.services import vote_weighted_avg
from aggregates import tasks as agg_tasks
from coaches.models import Coach
from core.models import PlatformSetting
from evaluations.models import EvaluationSession, ContextEvaluation, PlayerEvaluation, RefereeEvaluation
from leagues.models import League
from matches.models import Match, MatchPlayerStatistics, MatchTeamStatistics
from players.models import Player
from referees.models import Referee
from seasons.models import Season
from teams.models import Team
from users.models import SuspiciousActivityFlag

User = get_user_model()


class _Fixture(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")
        self.player = Player.objects.create(first_name="Иван", last_name="Тестов", team=self.home)
        self._n = 0

    def make_match(self, days_ago=0, status="finished", **extra):
        self._n += 1
        start = timezone.now() - timedelta(days=days_ago, minutes=self._n)
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status=status, start_time=start, end_time=start + timedelta(hours=2),
            voting_open_until=timezone.now() + timedelta(hours=48), **extra,
        )

    def make_user(self, name):
        return User.objects.create_user(username=name, email=f"{name}@example.com", password="pass12345")


class VoteWeightedAverageTests(_Fixture):
    def test_match_with_more_votes_weighs_more(self):
        m1, m2 = self.make_match(3), self.make_match(2)
        PlayerMatchAggregate.objects.create(player=self.player, match=m1, performance_score=8.0, total_votes=1)
        PlayerMatchAggregate.objects.create(player=self.player, match=m2, performance_score=4.0, total_votes=9)
        qs = PlayerMatchAggregate.objects.filter(player=self.player)

        weighted = qs.aggregate(v=vote_weighted_avg("performance_score"))["v"]
        plain = qs.aggregate(v=Avg("performance_score"))["v"]

        self.assertAlmostEqual(weighted, 4.4, places=3)  # (8*1 + 4*9) / 10
        self.assertAlmostEqual(plain, 6.0, places=3)

    def test_no_votes_gives_none_not_zero(self):
        m = self.make_match(1)
        PlayerMatchAggregate.objects.create(player=self.player, match=m, performance_score=7.0, total_votes=0)
        result = PlayerMatchAggregate.objects.filter(player=self.player).aggregate(
            v=vote_weighted_avg("performance_score")
        )["v"]
        self.assertIsNone(result)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class CorrectionAppliedTests(_Fixture):
    """Поправка пишется на агрегат, детектор видит чистую оценку."""

    def setUp(self):
        super().setUp()
        self.match = self.make_match(0)
        user = self.make_user("voter")
        ContextEvaluation.objects.create(user=user, match=self.match, watched_type="partial")
        # В рейтинг идут только голоса завершённых сессий.
        EvaluationSession.objects.create(
            user=user, match=self.match, status="completed", completed_at=timezone.now(),
        )
        PlayerEvaluation.objects.create(
            user=user, match=self.match, player=self.player, contribution=6, risk=3, potential=6
        )

    def test_correction_recorded_and_raw_score_restored(self):
        agg_tasks.recalculate_player_aggregates(str(self.match.id), apply_correction=False)
        clean = PlayerMatchAggregate.objects.get(player=self.player, match=self.match)
        self.assertAlmostEqual(clean.rating_correction_applied, 0.0)

        PlayerRatingCorrection.objects.create(player=self.player, correction=0.3)
        agg_tasks.recalculate_player_aggregates(str(self.match.id))
        corrected = PlayerMatchAggregate.objects.get(player=self.player, match=self.match)

        self.assertAlmostEqual(corrected.performance_score - clean.performance_score, 0.3, places=2)
        self.assertAlmostEqual(corrected.rating_correction_applied, 0.3, places=2)
        # Детектор видит ту же оценку, что без поправки.
        self.assertAlmostEqual(agg_tasks._raw_community_score(corrected), clean.performance_score, places=2)

    def test_history_recalc_without_correction(self):
        PlayerRatingCorrection.objects.create(player=self.player, correction=0.3)
        agg_tasks.recalculate_player_aggregates(str(self.match.id))
        call_command("recalculate_history_clean", apply=True, stdout=StringIO())
        agg = PlayerMatchAggregate.objects.get(player=self.player, match=self.match)
        self.assertAlmostEqual(agg.rating_correction_applied, 0.0)


class DominanceShareTests(_Fixture):
    def test_weighted_share_skips_missing_fields(self):
        m = self.make_match(0)
        own = MatchTeamStatistics(match=m, team=self.home, shots_on_goal=6, dangerous_attacks=40)
        opp = MatchTeamStatistics(match=m, team=self.away, shots_on_goal=2, dangerous_attacks=20)
        # (0.75*2.0 + (40/60)*1.5) / 3.5
        expected = (0.75 * 2.0 + (40 / 60) * 1.5) / 3.5
        self.assertAlmostEqual(agg_tasks._team_dominance_share(own, opp), expected, places=4)

    def test_no_common_data_returns_none(self):
        m = self.make_match(0)
        own = MatchTeamStatistics(match=m, team=self.home, shots_on_goal=6)
        opp = MatchTeamStatistics(match=m, team=self.away)
        self.assertIsNone(agg_tasks._team_dominance_share(own, opp))


class DivergenceFlagSyncTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.ct = ContentType.objects.get_for_model(Player)

    def test_pending_flag_is_updated_not_duplicated(self):
        agg_tasks._sync_divergence_flag(
            SuspiciousActivityFlag, self.ct, self.player.id, "player_stats_divergence", 0.5,
            {"pattern": "overrated_despite_stats", "correction_applied": -0.23},
        )
        first = SuspiciousActivityFlag.objects.get(source="player_stats_divergence")
        first_detected = first.details["first_detected_at"]

        agg_tasks._sync_divergence_flag(
            SuspiciousActivityFlag, self.ct, self.player.id, "player_stats_divergence", 0.6,
            {"pattern": "underrated_despite_stats", "correction_applied": 0.25},
        )
        flags = SuspiciousActivityFlag.objects.filter(source="player_stats_divergence")
        self.assertEqual(flags.count(), 1)
        flag = flags.get()
        self.assertEqual(flag.details["pattern"], "underrated_despite_stats")
        self.assertEqual(flag.details["correction_applied"], 0.25)
        self.assertEqual(flag.details["first_detected_at"], first_detected)
        self.assertAlmostEqual(flag.score, 0.6)

    def test_decay_marks_flag_inactive_with_current_correction(self):
        PlayerRatingCorrection.objects.create(player=self.player, correction=0.4, last_pattern="x")
        agg_tasks._sync_divergence_flag(
            SuspiciousActivityFlag, self.ct, self.player.id, "player_stats_divergence", 0.5,
            {"pattern": "underrated_despite_stats", "correction_applied": 0.4},
        )
        agg_tasks._decay_player_rating_correction(self.player.id, self.ct, SuspiciousActivityFlag)
        flag = SuspiciousActivityFlag.objects.get(source="player_stats_divergence")
        self.assertFalse(flag.details["pattern_active"])
        self.assertAlmostEqual(flag.details["correction_applied"], 0.2)
        # Поправка в антифроде = поправка на профиле игрока.
        self.assertAlmostEqual(flag.live_correction, 0.2)


class DismissalTests(_Fixture):
    def _flag(self):
        return SuspiciousActivityFlag.objects.create(
            content_type=ContentType.objects.get_for_model(Player), object_id=str(self.player.id),
            source="player_stats_divergence", score=0.5, details={"pattern": "overrated_despite_stats"},
        )

    def test_apply_divergence_dismissal_resets_correction_and_suppresses(self):
        PlayerRatingCorrection.objects.create(player=self.player, correction=-0.25, last_pattern="overrated_despite_stats")
        agg_tasks.apply_divergence_dismissal([self._flag()])
        corr = PlayerRatingCorrection.objects.get(player=self.player)
        self.assertEqual(corr.correction, 0.0)
        self.assertGreater(corr.suppressed_until, timezone.now())

    def test_dashboard_dismiss_button_removes_correction(self):
        """«Отклонить» в дашборде снимает поправку."""
        from dashboard import views as dashboard_views

        PlayerRatingCorrection.objects.create(player=self.player, correction=-0.25, last_pattern="overrated_despite_stats")
        flag = self._flag()
        admin = User.objects.create_superuser(username="boss", email="boss@example.com", password="pass12345")

        request = RequestFactory().post(f"/staff/dashboard/antifraud/{flag.id}/", {"action": "dismiss"})
        request.user = admin
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        setattr(request, "_messages", FallbackStorage(request))

        dashboard_views.antifraud_flag_action(request, flag.id)

        flag.refresh_from_db()
        self.assertEqual(flag.status, "dismissed")
        self.assertEqual(PlayerRatingCorrection.objects.get(player=self.player).correction, 0.0)


class PlayerDivergenceDetectorTests(_Fixture):
    """Детектор игрока: основной сигнал — RATING."""

    def setUp(self):
        super().setUp()
        self.ct = ContentType.objects.get_for_model(Player)

    def _history(self, window_score, window_applied=0.0):
        # 5 ранних матчей: болельщики 6.0, статистика 6.5
        for i in range(5):
            m = self.make_match(days_ago=20 + i)
            PlayerMatchAggregate.objects.create(player=self.player, match=m, performance_score=6.0, total_votes=10)
            MatchPlayerStatistics.objects.create(match=m, player=self.player, team=self.home, raw={"RATING": 6.5, "MINUTES_PLAYED": 90})
        # 5 последних: статистика 5.5, оценки — window_score
        for i in range(5):
            m = self.make_match(days_ago=1 + i)
            PlayerMatchAggregate.objects.create(
                player=self.player, match=m, performance_score=window_score, total_votes=10,
                rating_correction_applied=window_applied,
            )
            MatchPlayerStatistics.objects.create(match=m, player=self.player, team=self.home, raw={"RATING": 5.5, "MINUTES_PLAYED": 90})

    def test_overrated_uses_stat_rating_and_applies_negative_correction(self):
        self._history(window_score=7.5)
        result = agg_tasks._check_player_stats_divergence(self.player.id, self.ct, SuspiciousActivityFlag)
        self.assertEqual(result, 1)
        flag = SuspiciousActivityFlag.objects.get(source="player_stats_divergence")
        self.assertEqual(flag.details["pattern"], "overrated_despite_stats")
        self.assertEqual(flag.details["objective_source"], "sportmonks_rating")
        self.assertLess(PlayerRatingCorrection.objects.get(player=self.player).correction, 0)

    def test_own_correction_does_not_trigger_detector(self):
        """Рейтинг выше только из-за вшитой поправки — флага нет."""
        self._history(window_score=7.5, window_applied=1.5)  # чистая оценка 6.0
        result = agg_tasks._check_player_stats_divergence(self.player.id, self.ct, SuspiciousActivityFlag)
        self.assertEqual(result, 0)
        self.assertFalse(SuspiciousActivityFlag.objects.filter(source="player_stats_divergence").exists())


class CoachDivergenceTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.coach = Coach.objects.create(first_name="Тренер", last_name="Тренеров", team=self.home)
        self.ct = ContentType.objects.get_for_model(Coach)
        for i in range(6):  # ранние матчи: 6.0
            m = self.make_match(days_ago=30 + i)
            CoachMatchAggregate.objects.create(
                coach=self.coach, match=m, avg_tactics=6, avg_substitutions=6, avg_management=6, avg_impact=6, total_votes=10,
            )
        for i in range(6):  # последние: оценки 8.0, команда уступала
            m = self.make_match(days_ago=1 + i)
            CoachMatchAggregate.objects.create(
                coach=self.coach, match=m, avg_tactics=8, avg_substitutions=8, avg_management=8, avg_impact=8, total_votes=10,
            )
            MatchTeamStatistics.objects.create(match=m, team=self.home, shots_on_goal=1, dangerous_attacks=10)
            MatchTeamStatistics.objects.create(match=m, team=self.away, shots_on_goal=8, dangerous_attacks=60)

    def test_flag_created_without_rating_correction(self):
        self.assertEqual(agg_tasks._check_coach_stats_divergence(self.coach.id, self.ct, SuspiciousActivityFlag), 1)
        flag = SuspiciousActivityFlag.objects.get(source="coach_stats_divergence")
        self.assertEqual(flag.details["pattern"], "overrated_despite_poor_play")
        summary = flag.human_summary()
        self.assertIn("НЕ корректируются", summary["system_action"])

    def test_dismissed_coach_not_rechecked_for_30_days(self):
        agg_tasks._check_coach_stats_divergence(self.coach.id, self.ct, SuspiciousActivityFlag)
        SuspiciousActivityFlag.objects.filter(source="coach_stats_divergence").update(
            status="dismissed", reviewed_at=timezone.now()
        )
        self.assertEqual(agg_tasks._check_coach_stats_divergence(self.coach.id, self.ct, SuspiciousActivityFlag), 0)
        self.assertFalse(SuspiciousActivityFlag.objects.filter(source="coach_stats_divergence", status="pending").exists())


class RefereeVoteSpikeTests(_Fixture):
    def test_extreme_votes_against_referee_flagged(self):
        referee = Referee.objects.create(first_name="Судья", last_name="Судейский")
        users = [self.make_user(f"ref_voter_{i}") for i in range(8)]
        matches = [self.make_match(days_ago=i + 1, referee=referee) for i in range(6)]
        for idx, match in enumerate(matches):
            quality = 1 if idx == 0 else 6  # первый матч — «завалить судью»
            for u in users:
                RefereeEvaluation.objects.create(user=u, match=match, influence_score=50, decision_quality=quality)

        self.assertEqual(agg_tasks.detect_referee_vote_spikes_task(), 1)
        flag = SuspiciousActivityFlag.objects.get(source="vote_spike")
        self.assertEqual(flag.match_id, matches[0].id)
        self.assertEqual(flag.object_id, str(referee.id))
        self.assertIn("судейство", flag.human_summary()["explanation"].lower())

        # Повторный прогон не дублирует флаг.
        self.assertEqual(agg_tasks.detect_referee_vote_spikes_task(), 0)


class CalibrationTests(_Fixture):
    def test_calibration_saves_weights_and_formula_uses_them(self):
        from aggregates.management.commands import calibrate_player_objective_weights as cmd

        # Синтетика: оценка = 6 + 1.0*GOALS + 0.2*TACKLES
        players = [Player.objects.create(first_name=f"P{i}", last_name="X", team=self.home) for i in range(6)]
        for j in range(6):
            m = self.make_match(days_ago=j + 1)
            for i, p in enumerate(players):
                goals, tackles = (i + j) % 3, (i * j) % 5
                MatchPlayerStatistics.objects.create(
                    match=m, player=p, team=self.home,
                    raw={"GOALS": goals, "TACKLES": tackles, "MINUTES_PLAYED": 90, "RATING": 6 + goals + 0.2 * tackles},
                )

        # Сбрасываем кэш настроек после теста.
        self.addCleanup(cache.delete, "platform_setting:player_objective_weights")
        with mock.patch.object(cmd, "MIN_SAMPLES", 10):
            call_command("calibrate_player_objective_weights", save=True, stdout=StringIO())

        self.assertTrue(PlatformSetting.objects.filter(key="player_objective_weights").exists())
        m = self.make_match(0)
        MatchPlayerStatistics.objects.create(
            match=m, player=self.player, team=self.home, raw={"GOALS": 2, "TACKLES": 0, "MINUTES_PLAYED": 90}
        )
        score = agg_tasks._player_objective_score(m.id, self.player.id)
        self.assertAlmostEqual(score, 8.0, delta=0.15)
