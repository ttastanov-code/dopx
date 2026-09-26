# users/tests_progress.py
"""Незавершённая оценка никуда не идёт; после удаления сессий/прогнозов прогресс пересобирается."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from evaluations.models import ContextEvaluation, EvaluationSession
from evaluations.tests import _make_match
from predictions.models import MatchPrediction
from users.models import User, UserBadge, UserXP
from users.progress import recompute_user_progress


class RecomputeProgressTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fan", email="fan@example.com", password="x", is_verified=True, trust_score=1.25)

    def test_deleted_sessions_reset_counters_xp_and_badges(self):
        UserXP.objects.create(user=self.user, total_xp=50, level=2)
        self.user.total_evaluations, self.user.evaluation_streak, self.user.prediction_streak = 3, 3, 2
        self.user.save()
        UserBadge.objects.create(user=self.user, badge_type="first_evaluation")
        UserBadge.objects.create(user=self.user, badge_type="founder")
        recompute_user_progress(self.user)
        self.user.refresh_from_db()
        self.assertEqual((self.user.total_evaluations, self.user.evaluation_streak, self.user.prediction_streak), (0, 0, 0))
        self.assertEqual(self.user.xp.total_xp, 0)
        self.assertEqual(set(UserBadge.objects.filter(user=self.user).values_list("badge_type", flat=True)), {"founder"})

    def test_completed_session_gives_xp_and_first_badge_keeps_date(self):
        match = _make_match()
        EvaluationSession.objects.create(user=self.user, match=match, status="completed", completed_at=timezone.now())
        old = UserBadge.objects.create(user=self.user, badge_type="first_evaluation")
        UserBadge.objects.filter(pk=old.pk).update(awarded_at=timezone.now() - timedelta(days=30))
        recompute_user_progress(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.total_evaluations, 1)
        self.assertEqual(self.user.xp.total_xp, 7)  # 2+2+0 (нет состава)+1+1+1
        badge = UserBadge.objects.get(user=self.user, badge_type="first_evaluation")
        self.assertLess(badge.awarded_at, timezone.now() - timedelta(days=29))

    def test_prediction_streak_replayed(self):
        for home, away in ((1, 0), (2, 0), (0, 1)):
            match = _make_match()
            match.home_score, match.away_score = home, away
            match.save()
            MatchPrediction.objects.create(user=self.user, match=match, choice="1")
        recompute_user_progress(self.user)
        self.user.refresh_from_db()
        self.assertEqual(self.user.prediction_streak, 0)  # последний не угадан

    def test_session_delete_schedules_recompute(self):
        match = _make_match()
        session = EvaluationSession.objects.create(user=self.user, match=match, status="completed", completed_at=timezone.now())
        self.user.total_evaluations = 1
        self.user.save()
        with self.captureOnCommitCallbacks(execute=True):
            session.delete()
        self.user.refresh_from_db()
        self.assertEqual(self.user.total_evaluations, 0)


class CancelEvaluationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="c", email="c@example.com", password="x", is_verified=True)
        self.client.force_login(self.user)
        self.match = _make_match()

    def test_cancel_removes_draft(self):
        EvaluationSession.objects.create(user=self.user, match=self.match, status="in_progress", completed_steps=["context"])
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        response = self.client.post(reverse("evaluations:cancel", args=[self.match.id]))
        self.assertRedirects(response, reverse("matches:detail", args=[self.match.id]), fetch_redirect_response=False)
        self.assertFalse(EvaluationSession.objects.filter(user=self.user, match=self.match).exists())
        self.assertFalse(ContextEvaluation.objects.filter(user=self.user, match=self.match).exists())

    def test_completed_evaluation_not_cancellable(self):
        EvaluationSession.objects.create(user=self.user, match=self.match, status="completed")
        self.client.post(reverse("evaluations:cancel", args=[self.match.id]))
        self.assertTrue(EvaluationSession.objects.filter(user=self.user, match=self.match).exists())

    def test_get_not_allowed(self):
        self.assertEqual(self.client.get(reverse("evaluations:cancel", args=[self.match.id])).status_code, 405)
