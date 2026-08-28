# teams/tests.py
"""
teams/players/coaches/referees — четыре "справочных" приложения без единого
теста, при этом с общей переиспользуемой логикой (поиск с нормализацией
казахских букв-омографов core/utils.py::normalize_kz, гейт минимального
числа голосов MIN_VOTES_FOR_DISPLAY перед показом агрегата, сезонные
фильтры списков). Этот файл покрывает teams/views.py; players/coaches/
referees имеют свои tests.py с тем же общим паттерном, но проверяют только
ту функциональность, которая реально реализована у КАЖДОЙ конкретной сущности
(например, у referees нет сезонного фильтра списка — Referee не привязан к
TeamSeason, поэтому такого теста здесь для referees нет).

Регрессия, которая волнует больше всего: "Кайрат" (русские буквы) должен
находить "Қайрат" (казахские буквы) в поиске — этот баг чинили раньше в
dashboard/parser_tools.py, затем ту же логику вынесли в core/utils.py именно
потому, что баг оказался также в поиске команд/игроков/тренеров/судей (см.
докстринг core/utils.py). Тест на эту регрессию — приоритет №1 в каждом файле.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate, TeamMatchAggregate
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from leagues.models import League
from matches.models import Match
from players.models import Player
from seasons.models import Season
from teams.models import Team, TeamSeason


class TeamsFixtureMixin:
    """Главная лига + активный сезон — TeamListView по умолчанию (без
    ?season=all) показывает только команды, привязанные к TeamSeason
    активного сезона ГЛАВНОЙ лиги (Season.get_primary_active, см.
    teams/views.py::TeamListView.get_queryset)."""

    def setUp(self):
        self.league = League.objects.create(name="КПЛ", country="Казахстан", is_primary=True)
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)

    def _team_in_active_season(self, name):
        team = Team.objects.create(name=name)
        TeamSeason.objects.create(team=team, season=self.season)
        return team


class TeamSearchKazakhHomographTests(TeamsFixtureMixin, TestCase):
    """normalize_kz: "Кайрат" (русские буквы) должен находить "Қайрат"
    (казахские Қ/Ә/...) независимо от раскладки, которой набирали запрос."""

    def test_russian_spelling_finds_kazakh_named_team(self):
        team = self._team_in_active_season("Қайрат")
        response = self.client.get(reverse('teams:list'), {'q': 'Кайрат'})
        self.assertIn(team, response.context['teams'])

    def test_search_is_case_insensitive_after_normalization(self):
        team = self._team_in_active_season("Қайрат")
        response = self.client.get(reverse('teams:list'), {'q': 'кАйРаТ'})
        self.assertIn(team, response.context['teams'])

    def test_search_without_kazakh_letters_still_finds_exact_name(self):
        team = self._team_in_active_season("Астана")
        response = self.client.get(reverse('teams:list'), {'q': 'Астана'})
        self.assertIn(team, response.context['teams'])

    def test_no_matching_team_returns_empty_list(self):
        self._team_in_active_season("Қайрат")
        response = self.client.get(reverse('teams:list'), {'q': 'Барселона'})
        self.assertEqual(list(response.context['teams']), [])


class TeamListSeasonFilterTests(TeamsFixtureMixin, TestCase):
    """Дефолт: список показывает только команды текущего активного сезона
    главной лиги; ?season=all снимает фильтр (нужно, например, чтобы найти
    вылетевший клуб — см. комментарий в teams/views.py)."""

    def test_default_shows_only_teams_of_active_season(self):
        in_season = self._team_in_active_season("Тобол")
        outside_season = Team.objects.create(name="Вылетевший клуб")

        response = self.client.get(reverse('teams:list'))

        teams = list(response.context['teams'])
        self.assertIn(in_season, teams)
        self.assertNotIn(outside_season, teams)

    def test_season_all_shows_teams_outside_active_season_too(self):
        in_season = self._team_in_active_season("Тобол")
        outside_season = Team.objects.create(name="Вылетевший клуб")

        response = self.client.get(reverse('teams:list'), {'season': 'all'})

        teams = list(response.context['teams'])
        self.assertIn(in_season, teams)
        self.assertIn(outside_season, teams)
        self.assertTrue(response.context['show_all'])


class TeamDetailNotFoundTests(TestCase):
    def test_nonexistent_team_returns_404(self):
        response = self.client.get(reverse('teams:detail', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)

    def test_nonexistent_team_widget_returns_404(self):
        response = self.client.get(reverse('teams:widget', args=[uuid.uuid4()]))
        self.assertEqual(response.status_code, 404)


class TeamMatchFixtureMixin(TeamsFixtureMixin):
    def setUp(self):
        super().setUp()
        self.home = self._team_in_active_season("Хозяева")
        self.away = self._team_in_active_season("Гости")
        self.match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.home, away_team=self.away,
            status="finished",
            start_time=timezone.now() - timedelta(hours=3),
            voting_open_until=timezone.now() + timedelta(hours=45),
        )


class TeamRatingWidgetVoteGateTests(TeamMatchFixtureMixin, TestCase):
    """team_rating_widget: рейтинг показывается только когда total_votes
    (сумма голосов по TeamMatchAggregate) достиг MIN_VOTES_FOR_DISPLAY —
    иначе один голос "10/10 от друга" выглядел бы как настоящий консенсус."""

    def test_below_threshold_hides_rating(self):
        TeamMatchAggregate.objects.create(
            team=self.home, match=self.match,
            performance_score=9.0, total_votes=MIN_VOTES_FOR_DISPLAY - 1,
        )
        response = self.client.get(reverse('teams:widget', args=[self.home.id]))
        self.assertFalse(response.context['has_enough_votes'])

    def test_at_threshold_shows_rating(self):
        TeamMatchAggregate.objects.create(
            team=self.home, match=self.match,
            performance_score=9.0, total_votes=MIN_VOTES_FOR_DISPLAY,
        )
        response = self.client.get(reverse('teams:widget', args=[self.home.id]))
        self.assertTrue(response.context['has_enough_votes'])
        self.assertEqual(response.context['total_votes'], MIN_VOTES_FOR_DISPLAY)

    def test_no_aggregates_at_all_hides_rating_without_crashing(self):
        response = self.client.get(reverse('teams:widget', args=[self.home.id]))
        self.assertFalse(response.context['has_enough_votes'])
        self.assertEqual(response.context['total_votes'], 0)
        self.assertIsNone(response.context['avg_score'])


class TeamDetailTopPlayersGateTests(TeamMatchFixtureMixin, TestCase):
    """TeamDetailView.top_players фильтрует PlayerMatchAggregate по
    total_votes__gte=MIN_VOTES_FOR_DISPLAY — тот же гейт, что и на виджете,
    закрывает продуктовый аудит "доверие к рейтингу" (см. teams/views.py)."""

    def setUp(self):
        super().setUp()
        self.player = Player.objects.create(first_name="Игорь", last_name="Форвардов", team=self.home)

    def test_player_below_threshold_excluded_from_top_players(self):
        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=9.9, total_votes=MIN_VOTES_FOR_DISPLAY - 1,
        )
        response = self.client.get(reverse('teams:detail', args=[self.home.id]))
        self.assertEqual(list(response.context['top_players']), [])

    def test_player_at_threshold_included_in_top_players(self):
        aggregate = PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match,
            performance_score=9.9, total_votes=MIN_VOTES_FOR_DISPLAY,
        )
        response = self.client.get(reverse('teams:detail', args=[self.home.id]))
        self.assertIn(aggregate, list(response.context['top_players']))
