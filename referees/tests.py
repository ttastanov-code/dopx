# referees/tests.py
"""Тесты referees/views.py: поиск, матчи (факт) vs оценки (мнение), сезонный фильтр."""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.models import RefereeMatchAggregate
from leagues.models import League
from matches.models import Match
from referees.models import Referee
from seasons.models import Season
from teams.models import Team


class RefereeSearchKazakhHomographTests(TestCase):
    """«Гали» находит «Ғали»."""

    def test_russian_spelling_finds_kazakh_named_referee(self):
        referee = Referee.objects.create(first_name="Ғали", last_name="Өтегенов")
        response = self.client.get(reverse('referees:list'), {'q': 'Гали Отегенов'})
        self.assertIn(referee, response.context['referees'])

    def test_search_is_case_insensitive(self):
        referee = Referee.objects.create(first_name="Ғали", last_name="Өтегенов")
        response = self.client.get(reverse('referees:list'), {'q': 'гали'})
        self.assertIn(referee, response.context['referees'])

    def test_no_matching_referee_returns_empty_list(self):
        Referee.objects.create(first_name="Ғали", last_name="Өтегенов")
        response = self.client.get(reverse('referees:list'), {'q': 'Коллина'})
        self.assertEqual(list(response.context['referees']), [])


class RefereeListSearchConnectedTests(TestCase):
    """?q= фильтрует список."""

    def test_search_excludes_non_matching_referees(self):
        target = Referee.objects.create(first_name="Асан", last_name="Асанов")
        other = Referee.objects.create(first_name="Болат", last_name="Болатов")

        response = self.client.get(reverse('referees:list'), {'q': 'Асан'})

        referees = list(response.context['referees'])
        self.assertIn(target, referees)
        self.assertNotIn(other, referees)

    def test_inactive_referee_never_listed(self):
        Referee.objects.create(first_name="Неактивный", last_name="Судья", is_active=False)
        response = self.client.get(reverse('referees:list'))
        self.assertEqual(list(response.context['referees']), [])


class RefereeDetailNotFoundTests(TestCase):
    def test_nonexistent_referee_returns_404(self):
        response = self.client.get(reverse('referees:detail', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class RefereeMatchFixtureMixin:
    def setUp(self):
        self.league = League.objects.create(name="КПЛ", country="Казахстан", is_primary=True)
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.home = Team.objects.create(name="Хозяева")
        self.away = Team.objects.create(name="Гости")
        self.referee = Referee.objects.create(first_name="Судья", last_name="Тестовый")
        self.match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.home, away_team=self.away, referee=self.referee,
            status="finished",
            start_time=timezone.now() - timedelta(hours=3),
            voting_open_until=timezone.now() + timedelta(hours=45),
        )


class RefereeDetailFactsVsOpinionsTests(RefereeMatchFixtureMixin, TestCase):
    """Матч без оценок — в total_matches, но не в total_evaluations."""

    def test_refereed_match_without_evaluations_counts_as_match_not_as_evaluation(self):
        response = self.client.get(reverse('referees:detail', args=[self.referee.id]))
        stats = response.context['stats']
        self.assertEqual(stats['total_matches'], 1)
        self.assertEqual(stats['total_evaluations'], 0)


class RefereeDetailHasEvaluationsGateTests(RefereeMatchFixtureMixin, TestCase):
    """Карточка «Средние оценки» — при total_evaluations > 0."""

    def test_no_aggregate_gives_zero_evaluations(self):
        response = self.client.get(reverse('referees:detail', args=[self.referee.id]))
        self.assertEqual(response.context['stats']['total_evaluations'], 0)

    def test_aggregate_with_single_vote_is_already_counted(self):
        RefereeMatchAggregate.objects.create(
            referee=self.referee, match=self.match,
            avg_influence=5.0, avg_decision_quality=7.0, total_votes=1,
        )
        response = self.client.get(reverse('referees:detail', args=[self.referee.id]))
        self.assertEqual(response.context['stats']['total_evaluations'], 1)


class RefereeListVsDetailSeasonCountTests(TestCase):
    """Список и страница судьи считают матчи одинаково: 0 по умолчанию, 1 при ?season=all."""

    def setUp(self):
        self.league = League.objects.create(name="КПЛ", country="Казахстан", is_primary=True)
        self.active_season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.past_season = Season.objects.create(league=self.league, year="2025", is_active=False)
        self.home = Team.objects.create(name="Хозяева")
        self.away = Team.objects.create(name="Гости")
        self.referee = Referee.objects.create(first_name="Нурзатбек", last_name="Абдыкадыров")
        Match.objects.create(
            league=self.league, season=self.past_season,
            home_team=self.home, away_team=self.away, referee=self.referee,
            status="finished",
            start_time=timezone.now() - timedelta(days=400),
            voting_open_until=timezone.now() - timedelta(days=398),
        )

    def test_list_and_detail_agree_on_zero_by_default(self):
        list_response = self.client.get(reverse('referees:list'))
        referees = {r.id: r for r in list_response.context['referees']}
        self.assertEqual(referees[self.referee.id].total_matches, 0)

        detail_response = self.client.get(reverse('referees:detail', args=[self.referee.id]))
        self.assertEqual(detail_response.context['stats']['total_matches'], 0)

    def test_list_and_detail_agree_on_one_with_season_all(self):
        list_response = self.client.get(reverse('referees:list'), {'season': 'all'})
        referees = {r.id: r for r in list_response.context['referees']}
        self.assertEqual(referees[self.referee.id].total_matches, 1)

        detail_response = self.client.get(reverse('referees:detail', args=[self.referee.id]), {'season': 'all'})
        self.assertEqual(detail_response.context['stats']['total_matches'], 1)
