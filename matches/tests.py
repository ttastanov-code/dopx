# matches/tests.py
"""
Регрессионные тесты приложения `matches` — центрального приложения проекта
(карточки матчей на главной и в списке, детальная страница матча, статус
CTA "оценить матч"), у которого до этой сессии не было ни одного теста.

Особый фокус — `MatchListView.get_queryset()`: до недавнего времени фильтр
по статусу был построен на цепочке `if/elif`, где ветки для `postponed` и
`cancelled` отсутствовали вовсе — GET-параметры `?status=postponed` и
`?status=cancelled` молча проваливались в `else` и показывали ВСЕ матчи
без разбора статуса (см. комментарий "БАГ, КОТОРЫЙ ТУТ БЫЛ" в
matches/views.py). На момент написания этих тестов обе ветки в коде уже
есть — ниже не столько поиск нового бага, сколько регрессионный тест,
который заставит CI упасть, если кто-то снова "срежет" одну из веток
статуса при рефакторинге фильтра.

Второй фокус — `match_action_context()` (общий для MatchDetailView и
live-поллинга шапки матча `match_header_partial`): именно он решает,
показывать ли пользователю кнопку "оценить матч" — `voting_open`
истинно, только если матч `finished` И `voting_open_until` ещё не
наступил. Та же гейт-логика продублирована в `evaluations` (см.
evaluations/tests.py::VotingAccessGateTests) на уровне HTTP-редиректа —
здесь она тестируется на уровне контекста детальной страницы матча.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from evaluations.models import EvaluationSession
from events.models import EventReaction, MatchEvent
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team

User = get_user_model()


# ---------------------------------------------------------------------------
# Фабрики — тот же паттерн, что и в evaluations/tests.py::_make_match,
# только разбит на составные части, т.к. тестам фильтров нужно создавать
# несколько лиг/сезонов/команд в одном setUp.
# ---------------------------------------------------------------------------

def _make_league(**kwargs):
    defaults = {"name": f"League-{League.objects.count()}", "country": "KZ"}
    defaults.update(kwargs)
    return League.objects.create(**defaults)


def _make_season(league=None, **kwargs):
    league = league or _make_league()
    defaults = {"year": "2026"}
    defaults.update(kwargs)
    season, _created = Season.objects.get_or_create(league=league, **defaults)
    return season


def _make_team(**kwargs):
    defaults = {"name": f"Team-{Team.objects.count()}"}
    defaults.update(kwargs)
    return Team.objects.create(**defaults)


def _make_match(
    status="scheduled",
    start_time=None,
    voting_open_until=None,
    league=None,
    season=None,
    home_team=None,
    away_team=None,
    tour=None,
    **extra,
):
    league = league or _make_league()
    season = season or _make_season(league=league)
    home_team = home_team or _make_team()
    away_team = away_team or _make_team()
    start_time = start_time or (timezone.now() + timedelta(days=1))
    voting_open_until = voting_open_until or (timezone.now() + timedelta(hours=48))
    return Match.objects.create(
        league=league,
        season=season,
        home_team=home_team,
        away_team=away_team,
        start_time=start_time,
        voting_open_until=voting_open_until,
        status=status,
        tour=tour,
        **extra,
    )


# ---------------------------------------------------------------------------
# MatchListView — фильтр по статусу (см. докстринг модуля про elif-бага)
# ---------------------------------------------------------------------------

class MatchListViewStatusFilterTests(TestCase):
    """Каждое значение ?status=... должно возвращать РОВНО матчи этого
    статуса — ни одного лишнего из другого статуса и ни одного пропущенного."""

    def setUp(self):
        now = timezone.now()
        self.scheduled = _make_match(status="scheduled", start_time=now + timedelta(days=3))
        self.live = _make_match(status="live", start_time=now - timedelta(minutes=30))
        self.finished_open = _make_match(
            status="finished",
            start_time=now - timedelta(hours=3),
            voting_open_until=now + timedelta(hours=1),
        )
        self.postponed = _make_match(status="postponed", start_time=now + timedelta(days=10))
        self.cancelled = _make_match(status="cancelled", start_time=now - timedelta(days=1))
        # Отдельный finished-матч с УЖЕ закрытым голосованием — нужен, чтобы
        # отличить ?status=finished (должен включать оба finished-матча) от
        # ?status=votable (должен включать только тот, где голосование ещё
        # открыто).
        self.finished_closed = _make_match(
            status="finished",
            start_time=now - timedelta(days=5),
            voting_open_until=now - timedelta(hours=2),
        )

    def _ids(self, response):
        return {m.id for m in response.context["matches"]}

    def test_status_scheduled_returns_only_scheduled(self):
        response = self.client.get(reverse("matches:list"), {"status": "scheduled"})
        self.assertEqual(self._ids(response), {self.scheduled.id})

    def test_status_live_returns_only_live(self):
        response = self.client.get(reverse("matches:list"), {"status": "live"})
        self.assertEqual(self._ids(response), {self.live.id})

    def test_status_finished_returns_only_finished(self):
        response = self.client.get(reverse("matches:list"), {"status": "finished"})
        self.assertEqual(self._ids(response), {self.finished_open.id, self.finished_closed.id})

    def test_status_postponed_returns_only_postponed(self):
        """Регрессия на "БАГ, КОТОРЫЙ ТУТ БЫЛ": раньше эта ветка отсутствовала
        в if/elif, и ?status=postponed возвращал вообще все матчи."""
        response = self.client.get(reverse("matches:list"), {"status": "postponed"})
        self.assertEqual(self._ids(response), {self.postponed.id})

    def test_status_cancelled_returns_only_cancelled(self):
        """Та же регрессия, что и test_status_postponed_returns_only_postponed,
        но для второй пропавшей ветки — 'cancelled'."""
        response = self.client.get(reverse("matches:list"), {"status": "cancelled"})
        self.assertEqual(self._ids(response), {self.cancelled.id})

    def test_status_votable_returns_only_finished_with_open_voting(self):
        """?status=votable — то же условие, что и Match.is_voting_open():
        finished + voting_open_until ещё не наступил. finished_closed (тот же
        статус, но окно уже закрыто) обязан быть исключён."""
        response = self.client.get(reverse("matches:list"), {"status": "votable"})
        self.assertEqual(self._ids(response), {self.finished_open.id})

    def test_no_status_filter_returns_all_matches(self):
        """Без ?status= показываются матчи всех статусов (сортировка по
        близости к "сейчас" — сортировку отдельно не проверяем, только состав)."""
        response = self.client.get(reverse("matches:list"))
        self.assertEqual(
            self._ids(response),
            {
                self.scheduled.id, self.live.id, self.finished_open.id,
                self.postponed.id, self.cancelled.id, self.finished_closed.id,
            },
        )

    def test_unknown_status_value_falls_back_to_default_listing(self):
        """Мусорное значение ?status= не должно ронять страницу 500-й — оно
        просто не совпадает ни с одной веткой и проваливается в тот же
        default-branch, что и полное отсутствие параметра."""
        response = self.client.get(reverse("matches:list"), {"status": "bogus-value"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._ids(response),
            {
                self.scheduled.id, self.live.id, self.finished_open.id,
                self.postponed.id, self.cancelled.id, self.finished_closed.id,
            },
        )


# ---------------------------------------------------------------------------
# MatchListView — фильтры по лиге/сезону/туру (независимы от статуса)
# ---------------------------------------------------------------------------

class MatchListViewOtherFiltersTests(TestCase):
    """?league=/?season=/?tour= — накладываются ПОВЕРХ фильтра по статусу,
    каждый должен сужать список независимо от остальных."""

    def setUp(self):
        self.league_a = _make_league(name="League A")
        self.league_b = _make_league(name="League B")
        self.season_a = _make_season(league=self.league_a, year="2026")
        self.season_b = _make_season(league=self.league_b, year="2026")

        self.match_a = _make_match(league=self.league_a, season=self.season_a, tour=5)
        self.match_b = _make_match(league=self.league_b, season=self.season_b, tour=6)

    def test_filter_by_league(self):
        response = self.client.get(reverse("matches:list"), {"league": self.league_a.id})
        ids = {m.id for m in response.context["matches"]}
        self.assertEqual(ids, {self.match_a.id})

    def test_filter_by_season(self):
        response = self.client.get(reverse("matches:list"), {"season": self.season_b.id})
        ids = {m.id for m in response.context["matches"]}
        self.assertEqual(ids, {self.match_b.id})

    def test_filter_by_tour(self):
        response = self.client.get(reverse("matches:list"), {"tour": 5})
        ids = {m.id for m in response.context["matches"]}
        self.assertEqual(ids, {self.match_a.id})

    def test_combined_status_and_league_filters(self):
        other_in_league_a = _make_match(league=self.league_a, season=self.season_a, status="live")
        response = self.client.get(
            reverse("matches:list"), {"status": "scheduled", "league": self.league_a.id}
        )
        ids = {m.id for m in response.context["matches"]}
        # match_a — scheduled по умолчанию (см. _make_match), other_in_league_a — live,
        # должен быть отфильтрован и по статусу, и остаться той же лиги.
        self.assertEqual(ids, {self.match_a.id})
        self.assertNotIn(other_in_league_a.id, ids)


# ---------------------------------------------------------------------------
# MatchListView — стартовая страница по умолчанию (без ?page=/?status=)
# ---------------------------------------------------------------------------

class MatchListViewDefaultPaginationTests(TestCase):
    """
    НАЙДЕНО (2026-09-01, жалоба пользователя: "открывает на одну страницу
    раньше сегодняшней даты, показывает более старые матчи"):
    `paginate_queryset()` отступала на 3 позиции НАЗАД от индекса первого
    ещё не начавшегося матча — идея была показать чуть-чуть прошедших
    результатов вместе с будущими. При фиксированных страницах паджинатора
    (PAGINATE_BY=20) этот отступ иногда пересекал ГРАНИЦУ страницы целиком:
    если индекс первого будущего матча кратен 20 (или на 1-2 больше), минус
    3 уводит на ПРЕДЫДУЩУЮ страницу, где вообще нет ни одного будущего
    матча — только прошедшие. Тесты ниже закрывают именно граничный случай.
    """

    def setUp(self):
        self.league = _make_league()
        self.season = _make_season(league=self.league)
        self.home = _make_team()
        self.away = _make_team()

    def _match_at(self, offset_days):
        return _make_match(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            start_time=timezone.now() + timedelta(days=offset_days),
        )

    def test_default_page_contains_first_upcoming_match_at_page_boundary(self):
        """Ровно 40 прошедших матчей — индекс первого будущего (40) кратен
        PAGINATE_BY=20. До фикса открывалась 2-я страница (индексы 20-39,
        ВСЕ прошедшие) — первый будущий матч был виден только на 3-й."""
        for i in range(40, 0, -1):
            self._match_at(-i)
        first_upcoming = self._match_at(1)

        response = self.client.get(reverse("matches:list"))

        ids_on_page = [m.id for m in response.context["matches"]]
        self.assertIn(first_upcoming.id, ids_on_page)

    def test_default_page_contains_first_upcoming_match_off_boundary(self):
        """Несбойный случай (индекс первого будущего матча НЕ у границы
        страницы) — тоже должен работать, регрессия не должна ломать
        обычный путь."""
        for i in range(25, 0, -1):
            self._match_at(-i)
        first_upcoming = self._match_at(1)

        response = self.client.get(reverse("matches:list"))

        ids_on_page = [m.id for m in response.context["matches"]]
        self.assertIn(first_upcoming.id, ids_on_page)

    def test_no_past_matches_opens_first_page(self):
        only_future = self._match_at(1)

        response = self.client.get(reverse("matches:list"))

        self.assertEqual(response.context["page_obj"].number, 1)
        self.assertIn(only_future.id, [m.id for m in response.context["matches"]])


# ---------------------------------------------------------------------------
# MatchDetailView — 404 на несуществующий матч
# ---------------------------------------------------------------------------

class MatchDetailViewNotFoundTests(TestCase):
    def test_random_uuid_returns_404(self):
        response = self.client.get(reverse("matches:detail", kwargs={"pk": uuid.uuid4()}))
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# MatchDetailView — CTA "оценить матч" (match_action_context::voting_open)
# ---------------------------------------------------------------------------

class MatchDetailViewVotingGateContextTests(TestCase):
    """voting_open = match.status == 'finished' AND voting_open_until ещё не
    наступил. Проверяем все статусы и обе стороны временной границы."""

    def test_scheduled_match_voting_closed(self):
        match = _make_match(status="scheduled")
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["voting_open"])

    def test_live_match_voting_closed_even_with_future_deadline(self):
        """Статус 'live' с ещё не наступившим voting_open_until всё равно не
        должен показывать CTA — голосование открывается только после того,
        как матч реально завершён (status == 'finished')."""
        match = _make_match(status="live", voting_open_until=timezone.now() + timedelta(hours=48))
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertFalse(response.context["voting_open"])

    def test_postponed_match_voting_closed(self):
        match = _make_match(status="postponed")
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertFalse(response.context["voting_open"])

    def test_cancelled_match_voting_closed(self):
        match = _make_match(status="cancelled")
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertFalse(response.context["voting_open"])

    def test_finished_match_voting_just_opened(self):
        """Граница "только что открылось": voting_open_until ещё чуть
        впереди — CTA должен быть доступен."""
        match = _make_match(status="finished", voting_open_until=timezone.now() + timedelta(seconds=5))
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertTrue(response.context["voting_open"])

    def test_finished_match_voting_just_closed(self):
        """Граница "только что закрылось": voting_open_until только что
        прошёл — CTA должен исчезнуть."""
        match = _make_match(status="finished", voting_open_until=timezone.now() - timedelta(seconds=5))
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertFalse(response.context["voting_open"])

    def test_finished_match_voting_open_well_within_window(self):
        match = _make_match(status="finished", voting_open_until=timezone.now() + timedelta(hours=48))
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertTrue(response.context["voting_open"])

    def test_finished_match_voting_closed_long_ago(self):
        match = _make_match(status="finished", voting_open_until=timezone.now() - timedelta(days=30))
        response = self.client.get(reverse("matches:detail", kwargs={"pk": match.id}))
        self.assertFalse(response.context["voting_open"])


# ---------------------------------------------------------------------------
# MatchDetailView — user_has_evaluated / user_has_pulse_reactions
# ---------------------------------------------------------------------------

class MatchDetailViewUserFlagsTests(TestCase):
    """Флаги персонального состояния пользователя на странице матча —
    зависят и от статуса матча, и от того, авторизован ли пользователь."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.match = _make_match(status="finished", voting_open_until=timezone.now() + timedelta(hours=1))

    def test_anonymous_user_flags_are_false(self):
        response = self.client.get(reverse("matches:detail", kwargs={"pk": self.match.id}))
        self.assertFalse(response.context["user_has_evaluated"])
        self.assertFalse(response.context["user_has_pulse_reactions"])

    def test_user_has_evaluated_true_after_completed_session(self):
        EvaluationSession.objects.create(user=self.user, match=self.match, status="completed")
        self.client.force_login(self.user)
        response = self.client.get(reverse("matches:detail", kwargs={"pk": self.match.id}))
        self.assertTrue(response.context["user_has_evaluated"])

    def test_user_has_evaluated_false_without_completed_session(self):
        EvaluationSession.objects.create(user=self.user, match=self.match, status="in_progress")
        self.client.force_login(self.user)
        response = self.client.get(reverse("matches:detail", kwargs={"pk": self.match.id}))
        self.assertFalse(response.context["user_has_evaluated"])

    def test_user_has_pulse_reactions_true_when_reacted_and_not_evaluated(self):
        event = MatchEvent.objects.create(match=self.match, minute=10, event_type="goal", team_side="home")
        EventReaction.objects.create(match_event=event, user=self.user, reaction="like")
        self.client.force_login(self.user)
        response = self.client.get(reverse("matches:detail", kwargs={"pk": self.match.id}))
        self.assertTrue(response.context["user_has_pulse_reactions"])

    def test_user_has_pulse_reactions_not_checked_for_unfinished_match(self):
        """user_has_pulse_reactions считается только для finished-матчей
        (см. match_action_context) — на scheduled/live матче реакций на
        события в принципе быть не может, лишний запрос не нужен."""
        live_match = _make_match(status="live")
        event = MatchEvent.objects.create(match=live_match, minute=5, event_type="goal", team_side="away")
        EventReaction.objects.create(match_event=event, user=self.user, reaction="like")
        self.client.force_login(self.user)
        response = self.client.get(reverse("matches:detail", kwargs={"pk": live_match.id}))
        self.assertFalse(response.context["user_has_pulse_reactions"])
