# events/tests.py
"""
Регрессионные тесты live-пульса. Два реальных инцидента этой сессии:

  1. `react_to_event` возвращал HTTP 401 анонимному пользователю — HTMX по
     умолчанию не swap'ает контент ответов вне 2xx, поэтому тап анонима
     визуально не давал НИКАКОЙ обратной связи (см. docstring
     `events/views.py::react_to_event`). ReactToEventAnonymousTests закрывает
     именно этот сценарий через реальный HTTP-запрос, а не юнит-тест сервиса —
     баг был именно в статус-коде ответа, юнит-тест сервиса его бы не поймал.
  2. Вопрос "получается тут не будет храниться история реакций?" — по коду
     и по факту `EventReaction` НЕ имеет никакой зависимости от статуса
     матча: реакция, once persisted, остаётся навсегда, даже если матч
     потом сменил статус на `finished`. EventReactionPersistenceTests это
     фиксирует явно, чтобы это поведение не потерялось при следующем
     рефакторинге events/services.py.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import EventReaction, MatchEvent
from events.services import reaction_counts, toggle_reaction, user_reactions_map
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team

User = get_user_model()


def _make_match():
    league = League.objects.create(name="Test League", country="KZ")
    season = Season.objects.create(league=league, year="2026")
    home = Team.objects.create(name="Home")
    away = Team.objects.create(name="Away")
    return Match.objects.create(
        league=league, season=season, home_team=home, away_team=away,
        start_time=timezone.now(), voting_open_until=timezone.now() + timedelta(hours=48),
    )


class ToggleReactionServiceTests(TestCase):
    """Сервисный слой — идемпотентность тапа (create / toggle-off / switch)."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.match = _make_match()
        self.event = MatchEvent.objects.create(
            match=self.match, minute=10, event_type="goal", team_side="home"
        )

    def test_first_tap_creates_reaction(self):
        result = toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        self.assertEqual(result, "like")
        self.assertTrue(EventReaction.objects.filter(match_event=self.event, user=self.user, reaction="like").exists())

    def test_repeat_tap_same_reaction_removes_it(self):
        """Тап по 👍, потом ещё раз по 👍 — снимает реакцию (toggle-off),
        а не создаёт дубликат/падает на UniqueConstraint."""
        toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        result = toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        self.assertIsNone(result)
        self.assertFalse(EventReaction.objects.filter(match_event=self.event, user=self.user).exists())

    def test_tap_opposite_reaction_switches_not_duplicates(self):
        """👍 потом 👎 — ЗАМЕНЯЕТ реакцию, не создаёт вторую строку
        (нельзя одновременно лайкнуть и дизлайкнуть одно и то же событие)."""
        toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        result = toggle_reaction(user=self.user, match_event=self.event, reaction="dislike")
        self.assertEqual(result, "dislike")
        self.assertEqual(EventReaction.objects.filter(match_event=self.event, user=self.user).count(), 1)
        self.assertEqual(
            EventReaction.objects.get(match_event=self.event, user=self.user).reaction, "dislike"
        )

    def test_reaction_counts_aggregates_correctly(self):
        other = User.objects.create_user(username="u2", email="u2@example.com", password="pass123")
        toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        toggle_reaction(user=other, match_event=self.event, reaction="dislike")

        counts = reaction_counts([self.event.id])
        self.assertEqual(counts[self.event.id], {"like": 1, "dislike": 1})

    def test_user_reactions_map_only_this_user(self):
        other = User.objects.create_user(username="u2", email="u2@example.com", password="pass123")
        toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        toggle_reaction(user=other, match_event=self.event, reaction="dislike")

        mapping = user_reactions_map(self.user, [self.event.id])
        self.assertEqual(mapping, {self.event.id: "like"})


class ReactToEventAnonymousTests(TestCase):
    """ИСПРАВЛЕННЫЙ баг: анонимный тап должен получать 200 (с alternate-
    фрагментом "войдите"), а не 401 — иначе HTMX молча выбрасывает ответ."""

    def setUp(self):
        self.match = _make_match()
        self.event = MatchEvent.objects.create(
            match=self.match, minute=10, event_type="goal", team_side="home"
        )

    def test_anonymous_tap_returns_200_not_401(self):
        response = self.client.post(reverse("events:react", args=[self.event.id]), {"reaction": "like"})
        self.assertEqual(response.status_code, 200, "HTMX не swap'ает non-2xx ответы — анонимный тап должен быть 200")

    def test_anonymous_tap_does_not_persist_reaction(self):
        self.client.post(reverse("events:react", args=[self.event.id]), {"reaction": "like"})
        self.assertFalse(EventReaction.objects.filter(match_event=self.event).exists())


class ReactToEventAuthenticatedTests(TestCase):
    def setUp(self):
        self.match = _make_match()
        self.event = MatchEvent.objects.create(
            match=self.match, minute=10, event_type="goal", team_side="home"
        )
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        # force_login() вместо login(username=..., password=...): django-axes
        # (AxesBackend, см. dopx/settings.py::AUTHENTICATION_BACKENDS)
        # требует передавать `request` в authenticate() для учёта попыток
        # входа — client.login() его не передаёт и падает
        # AxesBackendRequestParameterRequired. force_login() ставит сессию
        # напрямую, минуя authenticate()/backends целиком — для теста
        # авторизованного тапа нам не нужно проверять сам логин-флоу
        # (он уже покрыт в users/tests.py), только то, что происходит
        # ПОСЛЕ успешной аутентификации.
        self.client.force_login(self.user)

    def test_authenticated_tap_returns_200_and_persists(self):
        response = self.client.post(reverse("events:react", args=[self.event.id]), {"reaction": "like"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(EventReaction.objects.filter(match_event=self.event, user=self.user, reaction="like").exists())

    def test_invalid_reaction_value_rejected(self):
        response = self.client.post(reverse("events:react", args=[self.event.id]), {"reaction": "love"})
        self.assertEqual(response.status_code, 405)
        self.assertFalse(EventReaction.objects.filter(match_event=self.event).exists())


class EventReactionPersistenceTests(TestCase):
    """Ответ на вопрос "получается тут не будет храниться история реакций?":
    EventReaction НЕ зависит от статуса матча — реакция переживает переход
    матча в 'finished' без какой-либо очистки/архивации."""

    def setUp(self):
        self.match = _make_match()
        self.event = MatchEvent.objects.create(
            match=self.match, minute=10, event_type="goal", team_side="home"
        )
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")

    def test_reaction_survives_match_status_change_to_finished(self):
        toggle_reaction(user=self.user, match_event=self.event, reaction="like")
        self.assertTrue(EventReaction.objects.filter(match_event=self.event, user=self.user).exists())

        self.match.status = "finished"
        self.match.save(update_fields=["status"])

        self.assertTrue(
            EventReaction.objects.filter(match_event=self.event, user=self.user).exists(),
            "реакция должна остаться в БД независимо от статуса родительского матча",
        )
