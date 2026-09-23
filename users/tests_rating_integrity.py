# users/tests_rating_integrity.py
"""
Тесты на исправления аудита достижений/серий/прогнозов (2026-09-23/24):
ранг «Первопроходца», серия оценок при оценке пропущенного тура, ничья в
«Против течения», переоценка статусных бейджей.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from leagues.models import League
from matches.models import Match
from predictions.models import MatchPrediction
from seasons.models import Season
from teams.models import Team
from users.models import UserBadge
from users.services import _maybe_award_against_the_tide, revalidate_status_badges
from users.tasks import award_founder_badge_if_eligible

User = get_user_model()


def _user(name, **extra):
    return User.objects.create_user(username=name, email=f"{name}@example.com", password="pass12345", **extra)


class _MatchFixture(TestCase):
    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.home = Team.objects.create(name="Home")
        self.away = Team.objects.create(name="Away")

    def make_match(self, tour=None, status="finished", home_score=0, away_score=1):
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status=status, tour=tour, home_score=home_score, away_score=away_score,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(hours=48),
        )


class FounderRankTests(TestCase):
    def test_rank_counts_unverified_earlier_registrations(self):
        """Регрессия: раньше ранг считался только среди верифицированных —
        поздний пользователь проскакивал в «первые N», пока ранние тянули
        с подтверждением почты."""
        base = timezone.now() - timedelta(days=10)
        early1 = _user("early1", is_verified=False)
        early2 = _user("early2", is_verified=False)
        late = _user("late", is_verified=True)
        for i, u in enumerate((early1, early2, late)):
            User.objects.filter(id=u.id).update(date_joined=base + timedelta(minutes=i))

        self.assertFalse(award_founder_badge_if_eligible.run(str(late.id), founder_threshold=2))
        self.assertFalse(UserBadge.objects.filter(user=late, badge_type="founder").exists())

        self.assertTrue(award_founder_badge_if_eligible.run(str(early1.id), founder_threshold=2))


class EvaluationStreakTests(_MatchFixture):
    def test_catching_up_earlier_tour_does_not_break_streak(self):
        """Регрессия: оценка пропущенного тура 4 после тура 6 откатывала
        last_evaluation_tour назад, и тур 7 уже считался разрывом серии."""
        user = _user("streaker")
        for tour in (5, 6):
            user.update_evaluation_stats(self.make_match(tour=tour))
        self.assertEqual(user.evaluation_streak, 2)

        user.update_evaluation_stats(self.make_match(tour=4))
        self.assertEqual(user.evaluation_streak, 2)
        self.assertEqual(user.last_evaluation_tour, 6)

        user.update_evaluation_stats(self.make_match(tour=7))
        self.assertEqual(user.evaluation_streak, 3)

    def test_real_gap_still_resets(self):
        user = _user("gapper")
        user.update_evaluation_stats(self.make_match(tour=5))
        user.update_evaluation_stats(self.make_match(tour=8))
        self.assertEqual(user.evaluation_streak, 1)


class AgainstTheTideTests(_MatchFixture):
    def _vote(self, match, choice, n, prefix):
        for i in range(n):
            MatchPrediction.objects.create(match=match, user=_user(f"{prefix}{i}"), choice=choice)

    def test_tie_between_leaders_is_not_against_the_tide(self):
        match = self.make_match(home_score=0, away_score=1)  # итог — "2"
        self._vote(match, "1", 3, "h")
        self._vote(match, "2", 2, "a")
        me = _user("me")
        MatchPrediction.objects.create(match=match, user=me, choice="2")  # 3 на 3 — ничья

        awarded = []
        _maybe_award_against_the_tide(me, awarded)
        self.assertEqual(awarded, [])

    def test_minority_correct_call_awarded(self):
        match = self.make_match(home_score=0, away_score=1)
        self._vote(match, "1", 4, "h")
        self._vote(match, "2", 1, "a")
        me = _user("me")
        MatchPrediction.objects.create(match=match, user=me, choice="2")

        awarded = []
        _maybe_award_against_the_tide(me, awarded)
        self.assertEqual([b.badge_type for b in awarded], ["against_the_tide"])


class StatusBadgeRevalidationTests(TestCase):
    def test_foresight_goes_stale_and_comes_back(self):
        user = _user("seer", trust_score=1.7, total_evaluations=40)
        badge = UserBadge.objects.create(user=user, badge_type="foresight")

        self.assertEqual(revalidate_status_badges(user), {"now_stale": [], "reactivated": []})

        user.trust_score = 1.0
        user.save(update_fields=["trust_score"])
        self.assertEqual(revalidate_status_badges(user)["now_stale"], ["foresight"])
        badge.refresh_from_db()
        self.assertTrue(badge.is_stale)
        self.assertIsNotNone(badge.stale_since)
        self.assertIn("неактуально", badge.tooltip_text)

        user.trust_score = 1.8
        user.save(update_fields=["trust_score"])
        self.assertEqual(revalidate_status_badges(user)["reactivated"], ["foresight"])
        badge.refresh_from_db()
        self.assertFalse(badge.is_stale)

    def test_milestone_badges_never_touched(self):
        user = _user("fan", total_evaluations=0)
        badge = UserBadge.objects.create(user=user, badge_type="active_fan_10")
        revalidate_status_badges(user)
        badge.refresh_from_db()
        self.assertFalse(badge.is_stale)
