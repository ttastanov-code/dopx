# coaches/tests.py
"""
Общий контекст — см. докстринг teams/tests.py. Здесь — coaches/views.py.

Особенности coaches относительно teams/players:
- CoachListView.get_context_data честно признаёт в комментарии БАГ,
  КОТОРЫЙ ТУТ БЫЛ: поиск (`q`) и фильтр по команде (`team`) рисовались в
  шаблоне, но queryset их не читал — форма молча ничего не делала. Код уже
  подключён (см. текущий текст views.py), но именно поэтому регрессионные
  тесты на оба параметра здесь особенно важны — это ровно тот баг, который
  легко случайно вернуть при следующей правке get_queryset.
- CoachDetailView.has_evaluations — НЕ MIN_VOTES_FOR_DISPLAY-гейт (в отличие
  от team_rating_widget/player_rating_widget). total_evaluations считается
  как Count('id') строк CoachMatchAggregate (число ОЦЕНЁННЫХ МАТЧЕЙ), не
  сумма голосов — карточка "Средние оценки" появляется, как только есть
  хотя бы один оценённый матч, независимо от числа голосов в нём.
- У тренера НЕТ личной истории матчей (KFF не хранит историю смены
  тренера — см. комментарий в coaches/views.py, "находка 4" в
  docs/BACKLOG.md) — team_matches сознательно берутся из МАТЧЕЙ КОМАНДЫ,
  это не баг для починки.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.models import CoachMatchAggregate
from coaches.models import Coach
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team, TeamSeason


class CoachesFixtureMixin:
    def setUp(self):
        self.league = League.objects.create(name="КПЛ", country="Казахстан", is_primary=True)
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team = Team.objects.create(name="Қайрат")
        TeamSeason.objects.create(team=self.team, season=self.season)

    def _coach(self, first_name, last_name, team=None, is_active=True):
        return Coach.objects.create(
            first_name=first_name, last_name=last_name,
            team=team if team is not None else self.team, is_active=is_active,
        )


class CoachSearchKazakhHomographTests(CoachesFixtureMixin, TestCase):
    """normalize_kz: "Кайрат"/"Гани" (русские буквы) должны находить
    тренера "Қайрат"/"Ғани" (казахские Қ/Ғ)."""

    def test_russian_spelling_finds_kazakh_named_coach(self):
        coach = self._coach("Ғани", "Қайратұлы")
        response = self.client.get(reverse('coaches:list'), {'q': 'Гани Кайратулы'})
        self.assertIn(coach, response.context['coaches'])

    def test_search_is_case_insensitive(self):
        coach = self._coach("Ғани", "Қайратұлы")
        response = self.client.get(reverse('coaches:list'), {'q': 'гани'})
        self.assertIn(coach, response.context['coaches'])

    def test_no_matching_coach_returns_empty_list(self):
        self._coach("Ғани", "Қайратұлы")
        response = self.client.get(reverse('coaches:list'), {'q': 'Моуринью'})
        self.assertEqual(list(response.context['coaches']), [])


class CoachListTeamFilterConnectedTests(CoachesFixtureMixin, TestCase):
    """Регрессия на конкретный БАГ, КОТОРЫЙ ТУТ БЫЛ (см. докстринг модуля):
    ?team=<id> должен реально фильтровать queryset, а не быть декоративным
    параметром, который шаблон рисует, но view игнорирует."""

    def test_team_filter_excludes_coaches_of_other_teams(self):
        own = self._coach("Свой", "Тренер")
        other_team = Team.objects.create(name="Другая команда")
        other = self._coach("Чужой", "Тренер", team=other_team)

        response = self.client.get(reverse('coaches:list'), {'team': str(self.team.id), 'season': 'all'})

        coaches = list(response.context['coaches'])
        self.assertIn(own, coaches)
        self.assertNotIn(other, coaches)


class CoachListSeasonFilterTests(CoachesFixtureMixin, TestCase):
    def test_default_shows_only_coaches_of_active_season_team(self):
        in_season = self._coach("Тренер", "Активного сезона")
        other_team = Team.objects.create(name="Вылетевший клуб")
        outside_season = self._coach("Тренер", "Вылетевшего клуба", team=other_team)

        response = self.client.get(reverse('coaches:list'))

        coaches = list(response.context['coaches'])
        self.assertIn(in_season, coaches)
        self.assertNotIn(outside_season, coaches)

    def test_season_all_shows_coaches_outside_active_season_too(self):
        in_season = self._coach("Тренер", "Активного сезона")
        other_team = Team.objects.create(name="Вылетевший клуб")
        outside_season = self._coach("Тренер", "Вылетевшего клуба", team=other_team)

        response = self.client.get(reverse('coaches:list'), {'season': 'all'})

        coaches = list(response.context['coaches'])
        self.assertIn(in_season, coaches)
        self.assertIn(outside_season, coaches)

    def test_inactive_coach_excluded_regardless_of_season(self):
        inactive = self._coach("Тренер", "Неактивный", is_active=False)
        response = self.client.get(reverse('coaches:list'), {'season': 'all'})
        self.assertNotIn(inactive, list(response.context['coaches']))


class CoachDetailNotFoundTests(TestCase):
    def test_nonexistent_coach_returns_404(self):
        response = self.client.get(reverse('coaches:detail', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class CoachDetailHasEvaluationsGateTests(CoachesFixtureMixin, TestCase):
    """has_evaluations = total_evaluations > 0, где total_evaluations —
    Count('id') строк CoachMatchAggregate (число оценённых МАТЧЕЙ), а не
    сумма голосов — в отличие от MIN_VOTES_FOR_DISPLAY-гейта у команд/
    игроков, здесь достаточно одного оценённого матча с любым total_votes."""

    def setUp(self):
        super().setUp()
        self.coach = self._coach("Тренер", "Тестовый")
        self.opponent = Team.objects.create(name="Соперник")
        self.match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            status="finished",
            start_time=timezone.now() - timedelta(hours=3),
            voting_open_until=timezone.now() + timedelta(hours=45),
        )

    def test_no_aggregates_has_evaluations_false(self):
        response = self.client.get(reverse('coaches:detail', args=[self.coach.id]))
        self.assertFalse(response.context['has_evaluations'])
        self.assertEqual(response.context['stats']['total_evaluations'], 0)

    def test_single_match_with_one_vote_still_counts_as_has_evaluations(self):
        CoachMatchAggregate.objects.create(
            coach=self.coach, match=self.match,
            avg_tactics=6.0, avg_substitutions=6.0, avg_management=6.0, avg_impact=6.0,
            total_votes=1,
        )
        response = self.client.get(reverse('coaches:detail', args=[self.coach.id]))
        self.assertTrue(response.context['has_evaluations'])
        self.assertEqual(response.context['stats']['total_evaluations'], 1)
