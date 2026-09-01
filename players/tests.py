# players/tests.py
"""
Общий контекст — см. докстринг teams/tests.py: четыре справочных приложения
(teams/players/coaches/referees) без единого теста, общая переиспользуемая
логика — normalize_kz-поиск, гейт MIN_VOTES_FOR_DISPLAY перед показом
агрегата, сезонный фильтр списков. Здесь — players/views.py.

У players, в отличие от teams, гейт голосов встречается ТРИЖДЫ по-разному:
- player_rating_widget: has_enough_votes по СУММЕ total_votes со ВСЕХ матчей
  игрока (PlayerMatchAggregate.objects.filter(player=player)).
- PlayerSeasonRecapView: has_enough_votes по сумме total_votes ТОЛЬКО за
  конкретный сезон (match__season=season) — тот же порог, другая выборка.
- PlayerDetailView.has_evaluations: НЕ использует MIN_VOTES_FOR_DISPLAY
  вообще — True как только у игрока есть хотя бы одна строка агрегата
  (evaluated_matches > 0), даже если голосов в ней меньше порога. Это не
  баг — виджет/season-recap показывают ЧИСЛО рейтинга, has_evaluations
  только решает, рисовать ли саму карточку "История выступлений".
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from leagues.models import League
from matches.models import Match
from players.models import Player
from seasons.models import Season
from teams.models import Team, TeamSeason


class PlayersFixtureMixin:
    """Главная лига + активный сезон — PlayerListView по умолчанию
    фильтрует по team__teamseason__season активного сезона главной лиги
    (тот же паттерн, что и TeamListView, см. players/views.py)."""

    def setUp(self):
        self.league = League.objects.create(name="КПЛ", country="Казахстан", is_primary=True)
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team = Team.objects.create(name="Қайрат")
        TeamSeason.objects.create(team=self.team, season=self.season)

    def _player(self, first_name, last_name, team=None, is_active=True):
        return Player.objects.create(
            first_name=first_name, last_name=last_name,
            team=team if team is not None else self.team, is_active=is_active,
        )


class PlayerSearchKazakhHomographTests(PlayersFixtureMixin, TestCase):
    """normalize_kz по first_name+last_name: "Нурлан" (русские буквы)
    должен находить игрока "Нұрлан" (казахская Ұ)."""

    def test_russian_spelling_finds_kazakh_named_player(self):
        player = self._player("Нұрлан", "Әбдіров")
        response = self.client.get(reverse('players:list'), {'q': 'Нурлан Абдиров'})
        self.assertIn(player, response.context['players'])

    def test_search_matches_last_name_only(self):
        player = self._player("Нұрлан", "Әбдіров")
        response = self.client.get(reverse('players:list'), {'q': 'Абдиров'})
        self.assertIn(player, response.context['players'])

    def test_search_is_case_insensitive(self):
        player = self._player("Нұрлан", "Әбдіров")
        response = self.client.get(reverse('players:list'), {'q': 'нурлан'})
        self.assertIn(player, response.context['players'])

    def test_no_matching_player_returns_empty_list(self):
        self._player("Нұрлан", "Әбдіров")
        response = self.client.get(reverse('players:list'), {'q': 'Криштиану'})
        self.assertEqual(list(response.context['players']), [])


class PlayerListSeasonFilterTests(PlayersFixtureMixin, TestCase):
    def test_default_shows_only_players_of_active_season_team(self):
        in_season = self._player("Игрок", "Активного сезона")
        other_team = Team.objects.create(name="Вылетевший клуб")
        outside_season = self._player("Игрок", "Вылетевшего клуба", team=other_team)

        response = self.client.get(reverse('players:list'))

        players = list(response.context['players'])
        self.assertIn(in_season, players)
        self.assertNotIn(outside_season, players)

    def test_season_all_shows_players_outside_active_season_too(self):
        in_season = self._player("Игрок", "Активного сезона")
        other_team = Team.objects.create(name="Вылетевший клуб")
        outside_season = self._player("Игрок", "Вылетевшего клуба", team=other_team)

        response = self.client.get(reverse('players:list'), {'season': 'all'})

        players = list(response.context['players'])
        self.assertIn(in_season, players)
        self.assertIn(outside_season, players)

    def test_inactive_player_still_shown_in_rating(self):
        """
        ПЕРЕВЁРНУТО (2026-09-01, жалоба пользователя): этот тест раньше
        закреплял ровно тот баг, который сейчас чиним. `is_active=False`
        проставляется photo_scraper'ом (parsers/kff/photo_scraper.py), когда
        KFF два прогона подряд не видит игрока в составе клуба — это сигнал
        для СОСТАВА КОМАНДЫ (teams/views.py), не для общего рейтинга.
        Игрок, сменивший клуб или покинувший КПЛ в середине сезона, не
        должен терять свои реальные оценки за уже сыгранные матчи — они
        никак не связаны с is_active (PlayerMatchAggregate). Рейтинг
        показывает его всегда, is_active используется только для бейджа
        "покинул клуб" в шаблоне.
        """
        inactive = self._player("Игрок", "Неактивный", is_active=False)
        response = self.client.get(reverse('players:list'), {'season': 'all'})
        self.assertIn(inactive, list(response.context['players']))

    def test_inactive_player_still_shown_within_active_season_too(self):
        """Тот же случай, но БЕЗ ?season=all — команда игрока (team FK)
        photo_scraper не обнуляет при уходе, она остаётся в текущем сезоне,
        поэтому обычный (не 'all') сезонный фильтр тоже должен его найти."""
        inactive = self._player("Игрок", "Неактивный", is_active=False)
        response = self.client.get(reverse('players:list'))
        self.assertIn(inactive, list(response.context['players']))


class PlayerDetailNotFoundTests(TestCase):
    def test_nonexistent_player_returns_404(self):
        response = self.client.get(reverse('players:detail', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)

    def test_nonexistent_player_widget_returns_404(self):
        response = self.client.get(reverse('players:widget', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class PlayerMatchFixtureMixin(PlayersFixtureMixin):
    def setUp(self):
        super().setUp()
        self.opponent = Team.objects.create(name="Соперник")
        self.player = self._player("Форвард", "Голеадоров")
        self.match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            status="finished",
            start_time=timezone.now() - timedelta(hours=3),
            voting_open_until=timezone.now() + timedelta(hours=45),
        )


class PlayerRatingWidgetVoteGateTests(PlayerMatchFixtureMixin, TestCase):
    """player_rating_widget: has_enough_votes по СУММЕ total_votes со всех
    матчей игрока — тот же порог MIN_VOTES_FOR_DISPLAY, что и у команд."""

    def test_below_threshold_hides_rating(self):
        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=8.5, total_votes=MIN_VOTES_FOR_DISPLAY - 1,
        )
        response = self.client.get(reverse('players:widget', args=[self.player.id]))
        self.assertFalse(response.context['has_enough_votes'])

    def test_at_threshold_shows_rating(self):
        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=8.5, total_votes=MIN_VOTES_FOR_DISPLAY,
        )
        response = self.client.get(reverse('players:widget', args=[self.player.id]))
        self.assertTrue(response.context['has_enough_votes'])


class PlayerSeasonRecapVoteGateTests(PlayerMatchFixtureMixin, TestCase):
    """PlayerSeasonRecapView: has_enough_votes считается ТОЛЬКО по агрегатам
    матчей ЭТОГО сезона (match__season=season) — агрегат другого сезона не
    должен утекать в сумму голосов текущего season recap."""

    def test_below_threshold_hides_average_performance_and_best_match(self):
        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=9.0, total_votes=MIN_VOTES_FOR_DISPLAY - 1,
        )
        response = self.client.get(reverse('players:season_recap', args=[self.player.id]))
        self.assertFalse(response.context['has_enough_votes'])
        self.assertIsNone(response.context['best_match'])

    def test_at_threshold_shows_average_performance_and_best_match(self):
        aggregate = PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=9.0, total_votes=MIN_VOTES_FOR_DISPLAY,
        )
        response = self.client.get(reverse('players:season_recap', args=[self.player.id]))
        self.assertTrue(response.context['has_enough_votes'])
        self.assertEqual(response.context['best_match'], aggregate)

    def test_aggregate_from_other_season_not_counted(self):
        other_league = League.objects.create(name="Другая лига", country="Казахстан")
        other_season = Season.objects.create(league=other_league, year="2020", is_active=False)
        other_match = Match.objects.create(
            league=other_league, season=other_season,
            home_team=self.team, away_team=self.opponent,
            status="finished",
            start_time=timezone.now() - timedelta(days=400),
            voting_open_until=timezone.now() - timedelta(days=398),
        )
        # Голоса за ПРОШЛЫЙ сезон — не должны попасть в recap ТЕКУЩЕГО активного сезона.
        PlayerMatchAggregate.objects.create(
            player=self.player, match=other_match,
            performance_score=9.9, total_votes=MIN_VOTES_FOR_DISPLAY,
        )
        response = self.client.get(reverse('players:season_recap', args=[self.player.id]))
        self.assertEqual(response.context['season'], self.season)
        self.assertFalse(response.context['has_enough_votes'])


class PlayerDetailHasEvaluationsTests(PlayerMatchFixtureMixin, TestCase):
    """PlayerDetailView.has_evaluations — НЕ гейтится MIN_VOTES_FOR_DISPLAY,
    достаточно хотя бы одной строки PlayerMatchAggregate (см. докстринг
    модуля) — отдельно от численного порога отображения рейтинга."""

    def test_no_aggregates_has_evaluations_false(self):
        response = self.client.get(reverse('players:detail', args=[self.player.id]))
        self.assertFalse(response.context['has_evaluations'])

    def test_single_low_vote_aggregate_still_counts_as_has_evaluations(self):
        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=7.0, total_votes=1,
        )
        response = self.client.get(reverse('players:detail', args=[self.player.id]))
        self.assertTrue(response.context['has_evaluations'])
