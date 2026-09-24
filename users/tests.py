# users/tests.py
"""Тесты users: кривая XP, достижения, антибот регистрации, аватарка, rate-limit.
CACHES -> LocMemCache, чтобы не зависеть от Redis.
"""
from __future__ import annotations

import time
from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from evaluations.models import EvaluationSession, PlayerEvaluation, RefereeEvaluation
from evaluations.models import CoachEvaluation
from coaches.models import Coach
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from players.models import Player
from predictions.models import MatchPrediction
from seasons.models import Season
from teams.models import Team
from matches.models import Match
from users.forms import MIN_FORM_FILL_SECONDS, UserProfileForm, UserRegistrationForm
from users.models import (
    UserBadge, UserXP, cumulative_xp_for_level, level_for_total_xp,
)
from users.services import check_and_award_badges

User = get_user_model()

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-users-rate-limiter",
    }
}


def _make_match(league=None, home=None, away=None):
    league = league or League.objects.create(name="Test League", country="KZ")
    # get_or_create — у Season уникальность (league, year).
    season, _created = Season.objects.get_or_create(league=league, year="2026")
    home = home or Team.objects.create(name="Home")
    away = away or Team.objects.create(name="Away")
    return Match.objects.create(
        league=league, season=season, home_team=home, away_team=away,
        start_time=timezone.now(), voting_open_until=timezone.now() + timedelta(hours=48),
    )


def _make_player(team=None):
    team = team or Team.objects.create(name=f"Team-{Team.objects.count()}")
    return Player.objects.create(first_name="First", last_name="Last", team=team)


# ---------------------------------------------------------------------------
# Кривая уровней/XP
# ---------------------------------------------------------------------------

class LevelCurveTests(TestCase):
    """cumulative_xp_for_level / level_for_total_xp."""

    def test_cumulative_xp_matches_documented_curve(self):
        # 2 уровень — 100 XP, 3 — 300, 4 — 600, 5 — 1000.
        self.assertEqual(cumulative_xp_for_level(1), 0)
        self.assertEqual(cumulative_xp_for_level(2), 100)
        self.assertEqual(cumulative_xp_for_level(3), 300)
        self.assertEqual(cumulative_xp_for_level(4), 600)
        self.assertEqual(cumulative_xp_for_level(5), 1000)

    def test_level_for_total_xp_below_first_threshold(self):
        self.assertEqual(level_for_total_xp(0), 1)
        self.assertEqual(level_for_total_xp(99), 1)

    def test_level_for_total_xp_exact_boundary_rounds_up(self):
        """Ровно на пороге — уже новый уровень."""
        self.assertEqual(level_for_total_xp(100), 2)
        self.assertEqual(level_for_total_xp(300), 3)

    def test_level_for_total_xp_just_below_boundary_stays_previous(self):
        self.assertEqual(level_for_total_xp(299), 2)
        self.assertEqual(level_for_total_xp(599), 3)

    def test_round_trip_stable_for_first_30_levels(self):
        """На пороге каждого уровня — именно этот уровень (погрешность float)."""
        for level in range(1, 31):
            threshold = cumulative_xp_for_level(level)
            self.assertEqual(
                level_for_total_xp(threshold), level,
                f"XP={threshold} (порог уровня {level}) вернул не тот уровень",
            )


class UserXPAddXPTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.xp = UserXP.objects.create(user=self.user)

    def test_add_xp_within_same_level_does_not_level_up(self):
        result = self.xp.add_xp(50)
        self.assertEqual(self.xp.total_xp, 50)
        self.assertEqual(self.xp.level, 1)
        self.assertFalse(result["level_increased"])
        self.assertEqual(result["levels_gained"], [])

    def test_add_xp_crossing_one_level_boundary(self):
        result = self.xp.add_xp(100)  # ровно порог 2 уровня
        self.assertEqual(self.xp.level, 2)
        self.assertTrue(result["level_increased"])
        self.assertEqual(result["levels_gained"], [2])

    def test_add_xp_crossing_multiple_levels_at_once(self):
        """Большой прирост перечисляет все пройденные уровни."""
        result = self.xp.add_xp(650)  # порог 4 уровня — 600
        self.assertEqual(self.xp.level, 4)
        self.assertEqual(result["levels_gained"], [2, 3, 4])

    def test_add_xp_never_goes_negative(self):
        self.xp.add_xp(20)
        self.xp.add_xp(-1000)
        self.assertEqual(self.xp.total_xp, 0)
        self.assertEqual(self.xp.level, 1)

    def test_progress_percent_zero_at_level_start(self):
        self.xp.add_xp(100)  # ровно порог 2 уровня — 0% прогресса
        self.assertEqual(self.xp.progress_percent, 0)

    def test_progress_percent_full_just_before_next_level(self):
        self.xp.add_xp(299)  # почти порог 3 уровня (300)
        self.assertGreaterEqual(self.xp.progress_percent, 90)
        self.assertLess(self.xp.progress_percent, 100)


class UserXpMultiplierAndTrustLevelTests(TestCase):
    """Вычисления над trust_score без БД."""

    def test_xp_multiplier_at_floor(self):
        self.assertEqual(User(trust_score=0.5).xp_multiplier(), 0.8)

    def test_xp_multiplier_at_ceiling(self):
        self.assertEqual(User(trust_score=2.0).xp_multiplier(), 1.2)

    def test_xp_multiplier_midpoint(self):
        self.assertEqual(User(trust_score=1.25).xp_multiplier(), 1.0)

    def test_xp_multiplier_clamps_below_floor(self):
        """Значение вне [0.5, 2.0] клампится."""
        self.assertEqual(User(trust_score=0.1).xp_multiplier(), User(trust_score=0.5).xp_multiplier())

    def test_xp_multiplier_clamps_above_ceiling(self):
        self.assertEqual(User(trust_score=5.0).xp_multiplier(), User(trust_score=2.0).xp_multiplier())

    def test_trust_level_thresholds(self):
        self.assertEqual(User(trust_score=1.8).get_trust_level()[0], "expert")
        self.assertEqual(User(trust_score=1.79).get_trust_level()[0], "reliable")
        self.assertEqual(User(trust_score=1.4).get_trust_level()[0], "reliable")
        self.assertEqual(User(trust_score=1.39).get_trust_level()[0], "standard")
        self.assertEqual(User(trust_score=1.0).get_trust_level()[0], "standard")
        self.assertEqual(User(trust_score=0.99).get_trust_level()[0], "new")


# ---------------------------------------------------------------------------
# Достижения
# ---------------------------------------------------------------------------

class CheckAndAwardBadgesCountThresholdTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")

    def _award(self):
        return {b.badge_type for b in check_and_award_badges(self.user)}

    def test_no_evaluations_no_badges(self):
        self.user.total_evaluations = 0
        self.assertEqual(self._award(), set())

    def test_first_evaluation_badge(self):
        self.user.total_evaluations = 1
        self.assertIn("first_evaluation", self._award())

    def test_active_fan_thresholds(self):
        self.user.total_evaluations = 50
        awarded = self._award()
        self.assertIn("active_fan_10", awarded)
        self.assertIn("active_fan_50", awarded)
        self.assertNotIn("active_fan_150", awarded)

    def test_streak_badges(self):
        self.user.evaluation_streak = 30
        awarded = self._award()
        self.assertIn("streak_7", awarded)
        self.assertIn("streak_30", awarded)
        self.assertNotIn("streak_100", awarded)

    def test_streak_just_below_threshold_not_awarded(self):
        self.user.evaluation_streak = 6
        self.assertNotIn("streak_7", self._award())

    def test_idempotent_second_call_returns_no_new_badges(self):
        """Повторный вызов не падает и не выдаёт бейджи повторно."""
        self.user.total_evaluations = 10
        first_call = self._award()
        self.assertIn("first_evaluation", first_call)
        second_call = self._award()
        self.assertEqual(second_call, set(), "повторный вызов не должен возвращать уже выданные бейджи")
        # Дублей в БД нет.
        self.assertEqual(UserBadge.objects.filter(user=self.user, badge_type="first_evaluation").count(), 1)

    def test_foresight_requires_both_volume_and_trust(self):
        self.user.total_evaluations = 30
        self.user.trust_score = 1.6
        self.assertIn("foresight", self._award())

    def test_foresight_not_awarded_with_high_trust_but_low_volume(self):
        self.user.total_evaluations = 5
        self.user.trust_score = 2.0
        self.assertNotIn("foresight", self._award())


class CheckAndAwardBadgesRelatedModelTests(TestCase):
    """judge_of_judges/polyglot — по реальным оценкам."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.league = League.objects.create(name="L", country="KZ")

    def test_judge_of_judges_awarded_at_25_referee_evaluations(self):
        self.user.total_evaluations = 25  # включает ветку judge_of_judges
        for _ in range(25):
            match = _make_match(league=self.league)
            RefereeEvaluation.objects.create(user=self.user, match=match, influence_score=50, decision_quality=5)
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertIn("judge_of_judges", awarded)

    def test_judge_of_judges_not_awarded_below_threshold(self):
        self.user.total_evaluations = 25
        for _ in range(24):
            match = _make_match(league=self.league)
            RefereeEvaluation.objects.create(user=self.user, match=match, influence_score=50, decision_quality=5)
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertNotIn("judge_of_judges", awarded)

    def test_polyglot_awarded_across_8_distinct_teams(self):
        self.user.total_evaluations = 10
        match = _make_match(league=self.league)
        for _ in range(8):
            team = Team.objects.create(name=f"Team-{Team.objects.count()}")
            player = _make_player(team=team)
            PlayerEvaluation.objects.create(
                user=self.user, match=match, player=player, contribution=5, risk=5, potential=5
            )
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertIn("polyglot", awarded)

    def test_polyglot_not_awarded_below_8_teams(self):
        self.user.total_evaluations = 10
        match = _make_match(league=self.league)
        for _ in range(7):
            team = Team.objects.create(name=f"Team-{Team.objects.count()}")
            player = _make_player(team=team)
            PlayerEvaluation.objects.create(
                user=self.user, match=match, player=player, contribution=5, risk=5, potential=5
            )
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertNotIn("polyglot", awarded)


# ---------------------------------------------------------------------------
# Новые достижения (включая legendary)
# ---------------------------------------------------------------------------

def _make_finished_match(league, season, tour=None, home=None, away=None, home_score=1, away_score=0):
    home = home or Team.objects.create(name=f"Home-{Team.objects.count()}")
    away = away or Team.objects.create(name=f"Away-{Team.objects.count()}")
    return Match.objects.create(
        league=league, season=season, home_team=home, away_team=away,
        tour=tour, status="finished", home_score=home_score, away_score=away_score,
        start_time=timezone.now() - timedelta(days=1),
        voting_open_until=timezone.now() + timedelta(hours=48),
    )


class CheckAndAwardBadgesNewAchievementsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.league = League.objects.create(name="L", country="KZ")
        self.season, _ = Season.objects.get_or_create(league=self.league, year="2026")

    def _award(self):
        return {b.badge_type for b in check_and_award_badges(self.user)}

    # --- coach_expert ---

    def test_coach_expert_awarded_at_25_coach_evaluations(self):
        self.user.total_evaluations = 25
        for _ in range(25):
            match = _make_finished_match(self.league, self.season)
            coach = Coach.objects.create(first_name="A", last_name="B", team=match.home_team)
            CoachEvaluation.objects.create(
                user=self.user, match=match, coach=coach,
                tactics=5, substitutions=5, game_management=5, impact=5,
            )
        self.assertIn("coach_expert", self._award())

    def test_coach_expert_not_awarded_below_threshold(self):
        self.user.total_evaluations = 25
        for _ in range(24):
            match = _make_finished_match(self.league, self.season)
            coach = Coach.objects.create(first_name="A", last_name="B", team=match.home_team)
            CoachEvaluation.objects.create(
                user=self.user, match=match, coach=coach,
                tactics=5, substitutions=5, game_management=5, impact=5,
            )
        self.assertNotIn("coach_expert", self._award())

    # --- both_sides ---

    def test_both_sides_awarded_when_both_teams_evaluated_in_15_matches(self):
        """Команда игрока на матч — через составы."""
        self.user.total_evaluations = 15
        for _ in range(15):
            match = _make_finished_match(self.league, self.season)
            home_lineup = MatchLineup.objects.create(match=match, team=match.home_team, side="home")
            away_lineup = MatchLineup.objects.create(match=match, team=match.away_team, side="away")
            home_player = Player.objects.create(first_name="H", last_name="P", team=match.home_team)
            away_player = Player.objects.create(first_name="A", last_name="P", team=match.away_team)
            MatchLineupPlayer.objects.create(lineup=home_lineup, player=home_player)
            MatchLineupPlayer.objects.create(lineup=away_lineup, player=away_player)
            PlayerEvaluation.objects.create(user=self.user, match=match, player=home_player, contribution=5, risk=5, potential=5)
            PlayerEvaluation.objects.create(user=self.user, match=match, player=away_player, contribution=5, risk=5, potential=5)
        self.assertIn("both_sides", self._award())

    def test_both_sides_not_awarded_when_only_one_team_evaluated(self):
        self.user.total_evaluations = 15
        for _ in range(15):
            match = _make_finished_match(self.league, self.season)
            home_lineup = MatchLineup.objects.create(match=match, team=match.home_team, side="home")
            home_player = Player.objects.create(first_name="H", last_name="P", team=match.home_team)
            MatchLineupPlayer.objects.create(lineup=home_lineup, player=home_player)
            PlayerEvaluation.objects.create(user=self.user, match=match, player=home_player, contribution=5, risk=5, potential=5)
        self.assertNotIn("both_sides", self._award())

    # --- full_season / season_completionist ---

    def test_full_season_awarded_when_every_tour_has_one_evaluated_match(self):
        self.user.total_evaluations = 10
        for tour in range(1, 11):  # FULL_SEASON_MIN_TOURS = 10
            match = _make_finished_match(self.league, self.season, tour=tour)
            EvaluationSession.objects.create(user=self.user, match=match, status="completed")
        self.assertIn("full_season", self._award())

    def test_full_season_not_awarded_with_one_missing_tour(self):
        # total_evaluations >= 10, иначе проверка не вызовется вовсе.
        self.user.total_evaluations = 10
        for tour in list(range(1, 10)) + [11]:  # тур 10 пропущен, туров в сезоне 10
            match = _make_finished_match(self.league, self.season, tour=tour)
            EvaluationSession.objects.create(user=self.user, match=match, status="completed")
        # 10-й тур существует, но не оценён.
        _make_finished_match(self.league, self.season, tour=10)
        self.assertNotIn("full_season", self._award())

    def test_season_completionist_awarded_when_every_match_evaluated(self):
        self.user.total_evaluations = 30
        for _ in range(30):  # SEASON_COMPLETIONIST_MIN_MATCHES = 30
            match = _make_finished_match(self.league, self.season)
            EvaluationSession.objects.create(user=self.user, match=match, status="completed")
        self.assertIn("season_completionist", self._award())

    def test_season_completionist_not_awarded_with_one_unevaluated_match(self):
        # total_evaluations >= 30, иначе проверка не вызовется вовсе.
        self.user.total_evaluations = 30
        for _ in range(30):
            match = _make_finished_match(self.league, self.season)
            EvaluationSession.objects.create(user=self.user, match=match, status="completed")
        _make_finished_match(self.league, self.season)  # 31-й матч не оценён
        self.assertNotIn("season_completionist", self._award())

    # --- stable_hand ---

    def test_stable_hand_awarded_at_50_predictions_85_percent_accuracy(self):
        for i in range(50):
            correct = i < 43  # 43/50 = 86% >= 85%
            match = _make_finished_match(self.league, self.season, home_score=1, away_score=0)  # final_result='1'
            MatchPrediction.objects.create(user=self.user, match=match, choice="1" if correct else "2")
        self.assertIn("stable_hand", self._award())

    def test_stable_hand_not_awarded_below_accuracy_threshold(self):
        for i in range(50):
            correct = i < 40  # 40/50 = 80% < 85%
            match = _make_finished_match(self.league, self.season, home_score=1, away_score=0)
            MatchPrediction.objects.create(user=self.user, match=match, choice="1" if correct else "2")
        self.assertNotIn("stable_hand", self._award())

    # --- derby_prophet ---

    def test_derby_prophet_awarded_for_5_correct_derby_predictions(self):
        rival_a = Team.objects.create(name="Rival A")
        rival_b = Team.objects.create(name="Rival B")
        rival_a.rivals.add(rival_b)
        for _ in range(5):
            match = _make_finished_match(
                self.league, self.season, home=rival_a, away=rival_b, home_score=2, away_score=0,
            )
            MatchPrediction.objects.create(user=self.user, match=match, choice="1")
        self.assertIn("derby_prophet", self._award())

    def test_derby_prophet_not_awarded_without_rival_teams(self):
        home = Team.objects.create(name="Home")
        away = Team.objects.create(name="Away")  # не соперники
        for _ in range(5):
            match = _make_finished_match(self.league, self.season, home=home, away=away, home_score=2, away_score=0)
            MatchPrediction.objects.create(user=self.user, match=match, choice="1")
        self.assertNotIn("derby_prophet", self._award())

    # --- against_the_tide ---

    def test_against_the_tide_awarded_for_correct_minority_pick(self):
        # Нужно >= 5 собственных прогнозов, иначе проверка не вызовется.
        for _ in range(4):
            filler = _make_finished_match(self.league, self.season, home_score=1, away_score=0)
            MatchPrediction.objects.create(user=self.user, match=filler, choice="1")
        match = _make_finished_match(self.league, self.season, home_score=0, away_score=1)  # final_result='2'
        MatchPrediction.objects.create(user=self.user, match=match, choice="2")  # меньшинство, но угадал
        for i in range(4):  # 4 из 5 на хозяев
            other = User.objects.create_user(username=f"other{i}", email=f"o{i}@example.com", password="x")
            MatchPrediction.objects.create(user=other, match=match, choice="1")
        self.assertIn("against_the_tide", self._award())

    def test_against_the_tide_not_awarded_when_pick_matches_majority(self):
        match = _make_finished_match(self.league, self.season, home_score=1, away_score=0)  # final_result='1'
        MatchPrediction.objects.create(user=self.user, match=match, choice="1")  # большинство и угадал
        for i in range(4):
            other = User.objects.create_user(username=f"other{i}", email=f"o{i}@example.com", password="x")
            MatchPrediction.objects.create(user=other, match=match, choice="1")
        self.assertNotIn("against_the_tide", self._award())

    # --- perfect_tour (legendary) ---

    def test_perfect_tour_awarded_when_all_tour_matches_predicted_correctly(self):
        for i in range(6):  # PERFECT_TOUR_MIN_MATCHES = 6
            match = _make_finished_match(self.league, self.season, tour=5, home_score=1, away_score=0)
            MatchPrediction.objects.create(user=self.user, match=match, choice="1")
        self.assertIn("perfect_tour", self._award())

    def test_perfect_tour_not_awarded_when_one_match_missed(self):
        for i in range(5):
            match = _make_finished_match(self.league, self.season, tour=5, home_score=1, away_score=0)
            MatchPrediction.objects.create(user=self.user, match=match, choice="1")
        _make_finished_match(self.league, self.season, tour=5, home_score=1, away_score=0)  # без прогноза
        self.assertNotIn("perfect_tour", self._award())

    def test_perfect_tour_not_awarded_when_one_prediction_wrong(self):
        for i in range(6):
            match = _make_finished_match(self.league, self.season, tour=5, home_score=1, away_score=0)
            choice = "1" if i < 5 else "2"  # последний прогноз неверный
            MatchPrediction.objects.create(user=self.user, match=match, choice=choice)
        self.assertNotIn("perfect_tour", self._award())

    # --- streak_250 / prediction_streak_200 (legendary) ---

    def test_streak_250_awarded_at_threshold(self):
        self.user.evaluation_streak = 250
        self.assertIn("streak_250", self._award())

    def test_streak_250_not_awarded_below_threshold(self):
        self.user.evaluation_streak = 249
        self.assertNotIn("streak_250", self._award())

    def test_prediction_streak_200_awarded_at_threshold(self):
        self.user.prediction_streak = 200
        self.assertIn("prediction_streak_200", self._award())

    def test_prediction_streak_200_not_awarded_below_threshold(self):
        self.user.prediction_streak = 199
        self.assertNotIn("prediction_streak_200", self._award())

    # --- max_trust (legendary) ---

    def test_max_trust_awarded_with_high_trust_and_volume(self):
        self.user.total_evaluations = 100
        self.user.trust_score = 1.95
        self.assertIn("max_trust", self._award())

    def test_max_trust_not_awarded_with_high_trust_but_low_volume(self):
        self.user.total_evaluations = 50
        self.user.trust_score = 2.0
        self.assertNotIn("max_trust", self._award())

    def test_max_trust_not_awarded_below_trust_threshold(self):
        self.user.total_evaluations = 100
        self.user.trust_score = 1.9
        self.assertNotIn("max_trust", self._award())


# ---------------------------------------------------------------------------
# Антибот регистрации: honeypot + time-trap
# ---------------------------------------------------------------------------

class RegistrationAntiFraudFormTests(TestCase):
    """Проверяем clean_*() напрямую — капча требует реального ответа."""

    def test_honeypot_empty_passes(self):
        form = UserRegistrationForm()
        form.cleaned_data = {"website": ""}
        self.assertEqual(form.clean_website(), "")

    def test_honeypot_filled_rejected(self):
        form = UserRegistrationForm()
        form.cleaned_data = {"website": "http://spam.example.com"}
        with self.assertRaises(ValidationError):
            form.clean_website()

    def test_time_trap_instant_submit_rejected(self):
        form = UserRegistrationForm()
        form.cleaned_data = {"form_rendered_at": time.time()}
        with self.assertRaises(ValidationError):
            form.clean_form_rendered_at()

    def test_time_trap_after_min_fill_seconds_passes(self):
        form = UserRegistrationForm()
        rendered_at = time.time() - MIN_FORM_FILL_SECONDS - 1
        form.cleaned_data = {"form_rendered_at": rendered_at}
        self.assertEqual(form.clean_form_rendered_at(), rendered_at)


# ---------------------------------------------------------------------------
# Валидация аватарки
# ---------------------------------------------------------------------------

def _valid_png_upload(name="avatar.png"):
    buf = BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class AvatarValidationFormTests(TestCase):
    def test_valid_png_passes(self):
        form = UserProfileForm()
        upload = _valid_png_upload()
        form.cleaned_data = {"avatar": upload}
        result = form.clean_avatar()
        self.assertIs(result, upload)

    def test_corrupt_file_rejected(self):
        form = UserProfileForm()
        fake = SimpleUploadedFile("avatar.jpg", b"this is not an image, just plain bytes", content_type="image/jpeg")
        form.cleaned_data = {"avatar": fake}
        with self.assertRaises(ValidationError):
            form.clean_avatar()

    @mock.patch("users.forms.MAX_AVATAR_SIZE_BYTES", 10)
    def test_oversized_file_rejected(self):
        """MAX_AVATAR_SIZE_BYTES = 10 байт, чтобы не создавать большой файл."""
        form = UserProfileForm()
        upload = _valid_png_upload()  # больше 10 байт
        form.cleaned_data = {"avatar": upload}
        with self.assertRaises(ValidationError):
            form.clean_avatar()

    def test_untouched_existing_avatar_not_revalidated(self):
        """Без нового файла Pillow.verify() не вызывается."""
        user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        user.avatar.name = "avatars/existing.png"
        form = UserProfileForm(instance=user)
        form.cleaned_data = {"avatar": user.avatar}
        result = form.clean_avatar()
        self.assertEqual(result, user.avatar)


# ---------------------------------------------------------------------------
# Rate-limit: password-reset, verify-email, toggle_follow
# ---------------------------------------------------------------------------

@override_settings(CACHES=LOCMEM_CACHES)
class PasswordResetRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        from users.views import PASSWORD_RESET_RATE_LIMIT
        self.limit = PASSWORD_RESET_RATE_LIMIT

    def test_exceeding_limit_redirects_with_error_instead_of_processing_form(self):
        url = reverse("users:password_reset")
        for _ in range(self.limit):
            self.client.post(url, {"email": "someone@example.com"})

        response = self.client.post(url, {"email": "flood@example.com"})
        self.assertRedirects(response, url)

        # Сообщение об ошибке — в next-ответе.
        response_followed = self.client.post(url, {"email": "flood2@example.com"}, follow=True)
        self.assertContains(response_followed, "Слишком много попыток")


@override_settings(CACHES=LOCMEM_CACHES)
class VerifyEmailRateLimitTests(TestCase):
    """Лимит сработал — пользователь не верифицирован, хотя токен валидный."""

    def setUp(self):
        cache.clear()
        from users.views import VERIFY_EMAIL_RATE_LIMIT
        self.limit = VERIFY_EMAIL_RATE_LIMIT
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=False)

    def test_exceeding_limit_blocks_before_user_lookup(self):
        import uuid

        # Лимит по IP — выбираем бакет чужим токеном, настоящий бережём до конца.
        bogus_url = reverse("users:verify_email", args=[uuid.uuid4()])
        for _ in range(self.limit):
            self.client.get(bogus_url)

        real_url = reverse("users:verify_email", args=[self.user.verification_token])
        self.client.get(real_url)  # (limit+1)-й запрос — блок
        self.user.refresh_from_db()
        self.assertFalse(
            self.user.is_verified,
            "запрос сверх лимита не должен был дойти до верификации пользователя",
        )

    def test_within_limit_verifies_user_normally(self):
        url = reverse("users:verify_email", args=[self.user.verification_token])
        self.client.get(url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)


@override_settings(CACHES=LOCMEM_CACHES)
class ToggleFollowRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        from users.views import FOLLOW_RATE_LIMIT
        self.limit = FOLLOW_RATE_LIMIT
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.player = _make_player()
        self.client.force_login(self.user)
        self.url = reverse("users:toggle_follow", args=["player", self.player.id])

    def test_exceeding_limit_returns_429_and_does_not_toggle(self):
        for _ in range(self.limit):
            self.client.post(self.url)

        from users.models import Follow
        state_before = Follow.objects.filter(user=self.user, player=self.player).exists()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 429)
        state_after = Follow.objects.filter(user=self.user, player=self.player).exists()
        self.assertEqual(state_before, state_after, "заблокированный запрос не должен менять состояние подписки")

    def test_within_limit_returns_200(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)


class BadgeShareCardViewTests(TestCase):
    """BadgeShareCardView: доступ и генерация PNG.
    'polyglot' — обычный бейдж, 'founder' — секретный.
    """

    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner", email="owner@example.com", password="pass123", is_profile_public=True,
        )
        self.other = User.objects.create_user(username="other", email="other@example.com", password="pass123")
        UserBadge.objects.create(user=self.owner, badge_type="polyglot")

    def test_owner_can_view_own_card_even_if_profile_private(self):
        """Владелец видит свою карточку и при приватном профиле."""
        self.owner.is_profile_public = False
        self.owner.save(update_fields=["is_profile_public"])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, "polyglot"]))
        self.assertEqual(response.status_code, 302)

    def test_other_user_can_view_public_profile_badge(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, "polyglot"]))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_can_view_public_profile_badge(self):
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, "polyglot"]))
        self.assertEqual(response.status_code, 302)

    def test_other_user_blocked_from_private_profile_badge(self):
        self.owner.is_profile_public = False
        self.owner.save(update_fields=["is_profile_public"])
        self.client.force_login(self.other)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, "polyglot"]))
        self.assertEqual(response.status_code, 404)

    def test_unknown_badge_code_404s(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, "not_a_real_code"]))
        self.assertEqual(response.status_code, 404)

    def test_unearned_badge_404s(self):
        # 'founder' не выдан — 404 из-за отсутствия UserBadge.
        self.client.force_login(self.owner)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, "founder"]))
        self.assertEqual(response.status_code, 404)

    def test_secret_badge_hidden_from_non_owner(self):
        from users.badges import BADGE_CATALOG
        secret_code = next(code for code, d in BADGE_CATALOG.items() if d.is_secret)
        UserBadge.objects.create(user=self.owner, badge_type=secret_code)
        self.client.force_login(self.other)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, secret_code]))
        self.assertEqual(response.status_code, 404)

    def test_secret_badge_visible_to_owner(self):
        from users.badges import BADGE_CATALOG
        secret_code = next(code for code, d in BADGE_CATALOG.items() if d.is_secret)
        UserBadge.objects.create(user=self.owner, badge_type=secret_code)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("users:badge_share_card", args=[self.owner.username, secret_code]))
        self.assertEqual(response.status_code, 302)


class ProfileEditViewOtpCollisionTests(TestCase):
    """Регрессия: OTPMiddleware подменяет request.user.is_verified на partial,
    сохранение профиля падало. Тест через self.client — с полным стеком middleware.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="profileeditor", email="profileeditor@example.com", password="pass123",
            is_verified=True,
        )
        self.client.force_login(self.user)
        self.url = reverse("users:profile_edit")

    def test_saving_profile_without_changing_email_does_not_crash(self):
        response = self.client.post(self.url, {
            "email": self.user.email,  # email не меняется
            "city": "Алматы",
            "bio": "Тест",
            "is_profile_public": "on",
        })
        self.assertEqual(response.status_code, 302)

        self.user.refresh_from_db()
        self.assertEqual(self.user.city, "Алматы")
        self.assertTrue(self.user.is_verified, "is_verified не должен был затронуться, раз email не менялся")

    def test_get_object_returns_fresh_instance_not_middleware_patched_request_user(self):
        # Имитируем OTPMiddleware: is_verified -> functools.partial.
        import functools
        poisoned_user = User.objects.get(pk=self.user.pk)
        poisoned_user.is_verified = functools.partial(lambda u: True, poisoned_user)

        from users.views import ProfileEditView
        view = ProfileEditView()
        view.request = type("R", (), {"user": poisoned_user})()
        obj = view.get_object()

        self.assertIsInstance(obj.is_verified, bool, "get_object() должен вернуть свежий экземпляр из БД, а не отравленный request.user")
