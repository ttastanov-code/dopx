# referees/tests.py
"""
Общий контекст — см. докстринг teams/tests.py. Здесь — referees/views.py.

Особенности referees относительно teams/players/coaches:
- Referee НЕ привязан к Team/TeamSeason — у судьи нет "своей команды"
  (см. referees/models.py), поэтому у RefereeListView НЕТ сезонного
  фильтра списка вообще (в отличие от TeamListView/PlayerListView/
  CoachListView) — соответствующего теста здесь намеренно нет, это не
  забытая фича, а отсутствующая по смыслу сущности.
- RefereeListView.get_queryset честно признаёт в комментарии БАГ, КОТОРЫЙ
  ТУТ БЫЛ: строка поиска рисовалась в шаблоне, но queryset её не читал —
  поиск был чисто декоративным. Код уже подключён, регрессионный тест на
  это — приоритет, чтобы будущая правка get_queryset не отключила поиск
  повторно.
- RefereeDetailView разделяет ФАКТ (total_matches — сколько матчей реально
  отсудил, из Match.objects.filter(referee=referee)) и МНЕНИЕ
  (total_evaluations — Sum(total_votes) по RefereeMatchAggregate). Гейт
  видимости карточки "Средние оценки" в шаблоне — total_evaluations > 0,
  а не MIN_VOTES_FOR_DISPLAY (в отличие от team_rating_widget/
  player_rating_widget), поэтому тестируем именно эту, отличную от
  команд/игроков, логику.
"""
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
    """normalize_kz: "Гали" (русские буквы) должен находить судью "Ғали"
    (казахская Ғ)."""

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
    """Регрессия на конкретный БАГ, КОТОРЫЙ ТУТ БЫЛ (см. докстринг модуля):
    ?q= должен реально фильтровать queryset, а не быть декоративным полем."""

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
    """total_matches (факт) и total_evaluations (мнение) — независимые
    числа: отсуженный матч без единой оценки болельщиков должен считаться
    в total_matches, но не создавать total_evaluations."""

    def test_refereed_match_without_evaluations_counts_as_match_not_as_evaluation(self):
        response = self.client.get(reverse('referees:detail', args=[self.referee.id]))
        stats = response.context['stats']
        self.assertEqual(stats['total_matches'], 1)
        self.assertEqual(stats['total_evaluations'], 0)


class RefereeDetailHasEvaluationsGateTests(RefereeMatchFixtureMixin, TestCase):
    """Карточка "Средние оценки" в шаблоне гейтится stats.total_evaluations
    > 0 (Sum(total_votes) по RefereeMatchAggregate), НЕ MIN_VOTES_FOR_DISPLAY
    — отличается от гейта на виджетах team/player."""

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
