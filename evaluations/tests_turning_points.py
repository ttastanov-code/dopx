# evaluations/tests_turning_points.py
"""Переломный момент: выбор в вайзарде, защита от чужих событий, топ в агрегате, API."""
from __future__ import annotations

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from aggregates.models import MatchAggregate
from aggregates.tasks import recalculate_match_aggregate
from evaluations.models import EvaluationSession, MatchEvaluation
from evaluations.tests import _make_match
from evaluations.turning_points import top_turning_points
from events.models import MatchEvent

User = get_user_model()
FINAL_POST = {"entertainment": 7, "tension": 6, "fairness": 8, "turning_point": "on"}


class TurningPointWizardTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fan", email="fan@example.com", password="x", is_verified=True)
        self.client.force_login(self.user)
        self.match = _make_match()
        self.goal = MatchEvent.objects.create(match=self.match, minute=90, added_time=5, event_type="goal", team_side="home")
        EvaluationSession.objects.create(
            user=self.user, match=self.match, status="in_progress",
            completed_steps=["context", "teams", "players", "coaches", "referee"],
        )
        self.url = reverse("evaluations:match_eval", args=[self.match.id])

    def test_page_lists_match_events_and_kinds(self):
        response = self.client.get(self.url)
        self.assertContains(response, f'value="event:{self.goal.id}"')
        self.assertContains(response, 'value="kind:referee_decision"')

    def test_event_choice_saved(self):
        self.client.post(self.url, {**FINAL_POST, "turning_point_choice": f"event:{self.goal.id}"})
        evaluation = MatchEvaluation.objects.get(user=self.user, match=self.match)
        self.assertEqual(evaluation.turning_point_event_id, self.goal.id)
        self.assertEqual(evaluation.turning_point_kind, "")

    def test_foreign_event_ignored(self):
        other = MatchEvent.objects.create(match=_make_match(), minute=10, event_type="goal", team_side="away")
        self.client.post(self.url, {**FINAL_POST, "turning_point_choice": f"event:{other.id}"})
        evaluation = MatchEvaluation.objects.get(user=self.user, match=self.match)
        self.assertIsNone(evaluation.turning_point_event_id)
        self.assertTrue(evaluation.turning_point)

    def test_choice_dropped_without_flag(self):
        post = {k: v for k, v in FINAL_POST.items() if k != "turning_point"}
        self.client.post(self.url, {**post, "turning_point_choice": "kind:tactics"})
        self.assertEqual(MatchEvaluation.objects.get(user=self.user, match=self.match).turning_point_kind, "")


class TopTurningPointsTests(TestCase):
    def test_counts_only_specified_and_orders_by_votes(self):
        make = lambda tp=True, kind="", event=None: SimpleNamespace(
            turning_point=tp, turning_point_kind=kind, turning_point_event_id=getattr(event, "id", None), turning_point_event=event,
        )
        match = _make_match()
        goal = MatchEvent.objects.create(match=match, minute=12, event_type="goal", team_side="home")
        evals = [make(event=goal), make(event=goal), make(kind="tactics"), make(), make(tp=False, kind="save")]
        top = top_turning_points(evals)
        self.assertEqual([t["pct"] for t in top], [67, 33])
        self.assertTrue(top[0]["label"].startswith("Гол 12'"))
        self.assertEqual(top[1]["label"], "Смена тактики")

    def test_aggregate_stores_top(self):
        match = _make_match()
        user = User.objects.create_user(username="v", email="v@example.com", password="x", is_verified=True)
        EvaluationSession.objects.create(user=user, match=match, status="completed")
        MatchEvaluation.objects.create(user=user, match=match, entertainment=7, tension=7, fairness=7,
                                       turning_point=True, turning_point_kind="save")
        recalculate_match_aggregate.run(str(match.id))
        self.assertEqual(MatchAggregate.objects.get(match=match).turning_points[0]["label"], "Сейв вратаря")
