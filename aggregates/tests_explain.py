# aggregates/tests_explain.py
"""Объяснение рейтинга: причины у игрока, история матча по реакциям, вывод на странице матча."""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.explain import explain_player, explain_players, match_story
from aggregates.models import PlayerMatchAggregate
from evaluations.models import EvaluationSession, PlayerEvaluation
from evaluations.tests import _make_match
from events.models import EventReaction, MatchEvent
from players.models import Player

User = get_user_model()


def _agg(**kw):
    base = dict(player_id=1, performance_score=7.5, own_fans_avg=None, rival_fans_avg=None)
    return SimpleNamespace(**{**base, **kw})


def _event(**kw):
    base = dict(id=10, player_id=1, assist_player_id=None, event_type="goal", display_minute="67", team_side="home")
    return SimpleNamespace(**{**base, **kw})


class ExplainPlayerTests(SimpleTestCase):
    def test_votes_distribution_and_headline(self):
        why = explain_player(_agg(performance_score=8.0), votes=[10, 9, 8, 8, 7, 5, 3])
        self.assertEqual(why["headline"], "7 болельщиков, 4 из них поставили 8 и выше")
        self.assertEqual(why["short"], "4 из 7 поставили 8 и выше")
        self.assertEqual([b["count"] for b in why["distribution"]], [2, 3, 1, 1])
        self.assertEqual(why["distribution"][1]["width"], 100)

    def test_smoothing_explained_when_far_from_plain_mean(self):
        why = explain_player(_agg(performance_score=7.0, own_fans_avg=9.0), votes=[9, 9, 9, 6])
        self.assertTrue(why["smoothing"].startswith("Простое среднее голосов — 8,2. Итог 7,0"))
        self.assertEqual(explain_player(_agg(performance_score=8.2), votes=[9, 9, 9, 6])["smoothing"], "")

    def test_sides(self):
        why = explain_player(_agg(own_fans_avg=8.4, rival_fans_avg=6.0, neutral_avg=7.1))
        self.assertEqual([(s["label"], s["value"]) for s in why["sides"]],
                         [("Болельщики его команды", 8.4), ("Болельщики соперника", 6.0), ("Нейтральные", 7.1)])

    def test_events_with_reactions_and_order(self):
        events = [_event(id=1, event_type="yellow_card", display_minute="20"), _event(id=2, display_minute="90+5"),
                  _event(id=3, player_id=2, assist_player_id=1, display_minute="30"), _event(id=4, player_id=3)]
        why = explain_player(_agg(), events=events, reactions={2: {"total": 20}, 3: {"total": 2}})
        self.assertEqual([f["text"] for f in why["factors"]], [
            "Забил гол на 90+5' — трибуны отреагировали 20 раз",
            "Отдал голевую передачу на 30'",
            "Жёлтая карточка на 20'",
        ])

    def test_compare_form_and_stats(self):
        why = explain_player(_agg(performance_score=8.0), form=(6.5, 4), stat_rating=6.8)
        self.assertEqual(why["compare"], ["Обычно в этом сезоне — 6,5. Этот матч лучше на 1,5.",
                                          "По статистике матча — 6,8."])
        self.assertEqual(explain_player(_agg(), form=(5.0, 1))["compare"], [])

    def test_expert(self):
        take = SimpleNamespace(display_name="Иван Петров", headline="Тащил вторую половину")
        why = explain_player(_agg(), expert_take=take)
        self.assertEqual(why["factors"][0]["text"],
                         "Ключевой игрок матча по мнению эксперта (Иван Петров): «Тащил вторую половину»")

    def test_empty(self):
        self.assertFalse(explain_player(_agg())["has_details"])


class ExplainDbTests(TestCase):
    def setUp(self):
        self.match = _make_match(voting_open_until=timezone.now() - timedelta(hours=1))
        self.player = Player.objects.create(first_name="Иван", last_name="Гол", team=self.match.home_team)
        self.goal = MatchEvent.objects.create(match=self.match, minute=67, event_type="goal", team_side="home",
                                              player=self.player)
        self.users = [User.objects.create_user(username=f"u{i}", email=f"u{i}@ex.com", password="x") for i in range(9)]
        for i, user in enumerate(self.users):
            EventReaction.objects.create(match_event=self.goal, user=user, reaction="like" if i < 7 else "dislike")
        self.agg = PlayerMatchAggregate.objects.create(player=self.player, match=self.match, performance_score=8.2,
                                                       total_votes=9, own_fans_avg=8.5, rival_fans_avg=8.0)

    def test_season_form_and_drift(self):
        # Прошлые матчи сезона: средняя 6.5.
        for score in (6.0, 7.0):
            past = _make_match(voting_open_until=timezone.now() - timedelta(days=3))
            past.season = self.match.season
            past.save(update_fields=["season"])
            PlayerMatchAggregate.objects.create(player=self.player, match=past, performance_score=score, total_votes=9)
        for i, user in enumerate(self.users):
            EvaluationSession.objects.create(user=user, match=self.match, status="completed")
            PlayerEvaluation.objects.create(user=user, match=self.match, player=self.player,
                                            contribution=6 if i < 3 else 9, risk=3, potential=7)

        explain_players(self.match, [self.agg], events=[self.goal])
        why = self.agg.why
        self.assertEqual(why["factors"][0]["text"], "Забил гол на 67' — трибуны отреагировали 9 раз")
        self.assertEqual(why["compare"][0], "Обычно в этом сезоне — 6,5. Этот матч лучше на 1,7.")
        self.assertEqual(why["headline"], "9 болельщиков, 6 из них поставили 8 и выше")

    def test_match_story_loudest_and_divisive(self):
        card = MatchEvent.objects.create(match=self.match, minute=80, event_type="red_card", team_side="away")
        voters = self.users + [User.objects.create_user(username=f"x{i}", email=f"x{i}@ex.com", password="x")
                               for i in range(3)]
        for i, user in enumerate(voters):
            EventReaction.objects.create(match_event=card, user=user, reaction="like" if i % 2 else "dislike")
        story = match_story(self.match, [self.goal, card],
                            turning_points=[{"key": "kind:save", "label": "Сейв вратаря", "pct": 60}])
        self.assertEqual(story["loudest_id"], card.id)
        self.assertEqual(story["summary"][0], "Переломом болельщики назвали: Сейв вратаря (60% ответов).")
        self.assertEqual(story["summary"][1], "Самый громкий момент: красная карточка на 80'.")
        self.assertEqual(len(story["moments"]), 2)

    def test_story_none_without_key_events(self):
        sub = MatchEvent.objects.create(match=self.match, minute=50, event_type="substitution", team_side="home")
        self.assertIsNone(match_story(self.match, [sub]))

    def test_detail_page_shows_explanation(self):
        response = self.client.get(reverse("matches:detail", args=[self.match.id]))
        self.assertContains(response, "Откуда 8,2")
        self.assertContains(response, "Забил гол на 67&#x27; — трибуны отреагировали 9 раз")
        self.assertContains(response, "Как трибуны прожили матч")


class ExplainHistoryTests(TestCase):
    def test_delta_and_reasons_on_player_page(self):
        closed = timezone.now() - timedelta(hours=1)
        first = _make_match(voting_open_until=closed)
        first.start_time = timezone.now() - timedelta(days=7)
        first.save(update_fields=["start_time"])
        second = _make_match(voting_open_until=closed)
        second.season = first.season
        second.save(update_fields=["season"])
        player = Player.objects.create(first_name="Пётр", last_name="Форма", team=second.home_team)
        MatchEvent.objects.create(match=second, minute=12, event_type="goal", team_side="home", player=player)
        PlayerMatchAggregate.objects.create(player=player, match=first, performance_score=6.0, total_votes=9)
        PlayerMatchAggregate.objects.create(player=player, match=second, performance_score=7.4, total_votes=9)

        response = self.client.get(reverse("players:detail", args=[player.id]))
        rows = {a.match_id: a for a in response.context["aggregates"]}
        self.assertEqual(rows[second.id].delta, 1.4)
        self.assertFalse(hasattr(rows[first.id], "delta"))
        self.assertEqual(rows[second.id].why["factors"][0]["text"], "Забил гол на 12'")
        self.assertContains(response, "md-delta--up")
