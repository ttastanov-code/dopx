# aggregates/tests.py
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta
from evaluations.models import (
    PlayerEvaluation, ContextEvaluation, MatchEvaluation
)
from matches.models import Match
from teams.models import Team
from players.models import Player
from seasons.models import Season
from leagues.models import League
from .models import PlayerMatchAggregate, MatchAggregate
from .services import (
    calculate_user_weight,
    recalculate_player_aggregate,
    recalculate_match_aggregate,
    recalculate_all_aggregates_for_match
)

User = get_user_model()


class UserWeightTests(TestCase):
    """Тесты расчёта веса голоса пользователя"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123',
            trust_score=1.0
        )
        self.league = League.objects.create(name='Test League', country='KZ')
        self.season = Season.objects.create(league=self.league, year='2026')
        self.team1 = Team.objects.create(name='Team 1')
        self.team2 = Team.objects.create(name='Team 2')
        self.match = Match.objects.create(
            league=self.league,
            season=self.season,
            home_team=self.team1,
            away_team=self.team2,
            start_time=timezone.now(),
            voting_open_until=timezone.now() + timedelta(hours=48)
        )
    
    def test_base_weight(self):
        """Базовый вес = 1.0"""
        context = ContextEvaluation.objects.create(
            user=self.user,
            match=self.match,
            watched_type='partial'
        )
        weight = calculate_user_weight(self.user, context)
        self.assertEqual(weight, 1.0)
    
    def test_full_match_bonus(self):
        """Бонус +0.2 за полный матч"""
        context = ContextEvaluation.objects.create(
            user=self.user,
            match=self.match,
            watched_type='full'
        )
        weight = calculate_user_weight(self.user, context)
        self.assertEqual(weight, 1.2)
    
    def test_trust_score_bonus(self):
        """Бонус +0.2 за trust_score > 1.2"""
        self.user.trust_score = 1.3
        self.user.save()
        context = ContextEvaluation.objects.create(
            user=self.user,
            match=self.match,
            watched_type='partial'
        )
        weight = calculate_user_weight(self.user, context)
        self.assertEqual(weight, 1.2)
    
    def test_combined_bonuses(self):
        """Комбинированные бонусы"""
        self.user.trust_score = 1.3
        self.user.save()
        context = ContextEvaluation.objects.create(
            user=self.user,
            match=self.match,
            watched_type='full'
        )
        weight = calculate_user_weight(self.user, context)
        self.assertEqual(weight, 1.4)


class AggregateCalculationTests(TestCase):
    """Тесты расчёта агрегатов"""
    
    def setUp(self):
        self.user1 = User.objects.create_user(
            username='user1',
            email='user1@example.com',
            password='pass123',
            trust_score=1.0
        )
        self.user2 = User.objects.create_user(
            username='user2',
            email='user2@example.com',
            password='pass123',
            trust_score=1.3  # Высокий trust
        )
        
        self.league = League.objects.create(name='Test League', country='KZ')
        self.season = Season.objects.create(league=self.league, year='2026')
        self.team1 = Team.objects.create(name='Team 1')
        self.team2 = Team.objects.create(name='Team 2')
        self.match = Match.objects.create(
            league=self.league,
            season=self.season,
            home_team=self.team1,
            away_team=self.team2,
            start_time=timezone.now(),
            voting_open_until=timezone.now() + timedelta(hours=48)
        )
        self.player = Player.objects.create(
            first_name='Test',
            last_name='Player',
            team=self.team1
        )
    
    def test_player_aggregate_calculation(self):
        """Расчёт агрегатов игрока"""
        # Создаём context evaluations
        ContextEvaluation.objects.create(
            user=self.user1, match=self.match, watched_type='partial'
        )
        ContextEvaluation.objects.create(
            user=self.user2, match=self.match, watched_type='full'
        )
        
        # Создаём оценки игроков
        PlayerEvaluation.objects.create(
            user=self.user1, match=self.match, player=self.player,
            contribution=7, risk=3, potential=8
        )
        PlayerEvaluation.objects.create(
            user=self.user2, match=self.match, player=self.player,
            contribution=9, risk=2, potential=9
        )
        
        # Пересчитываем агрегаты
        aggregate = recalculate_player_aggregate(self.player, self.match)
        
        self.assertIsNotNone(aggregate)
        self.assertEqual(aggregate.total_votes, 2)
        # Взвешенное среднее: user1 (weight=1.0), user2 (weight=1.4)
        # contribution: (7*1.0 + 9*1.4) / 2.4 = 19.6/2.4 = 8.17
        self.assertAlmostEqual(aggregate.avg_contribution, 8.17, places=1)
        self.assertEqual(aggregate.performance_score, aggregate.avg_contribution)
        self.assertGreater(aggregate.maturity_score, 0)  # contribution - risk
    
    def test_match_aggregate_drama_index(self):
        """Расчёт drama index матча"""
        ContextEvaluation.objects.create(
            user=self.user1, match=self.match, watched_type='full'
        )
        
        MatchEvaluation.objects.create(
            user=self.user1, match=self.match,
            entertainment=8, tension=9, fairness=7
        )
        
        aggregate = recalculate_match_aggregate(self.match)
        
        self.assertIsNotNone(aggregate)
        # drama_index = entertainment * tension = 8 * 9 = 72
        self.assertEqual(aggregate.drama_index, 72.0)
    
    def test_recalculate_all_aggregates(self):
        """Пересчёт всех агрегатов для матча"""
        ContextEvaluation.objects.create(
            user=self.user1, match=self.match, watched_type='full'
        )
        
        PlayerEvaluation.objects.create(
            user=self.user1, match=self.match, player=self.player,
            contribution=8, risk=3, potential=7
        )
        
        MatchEvaluation.objects.create(
            user=self.user1, match=self.match,
            entertainment=8, tension=7, fairness=8
        )
        
        result = recalculate_all_aggregates_for_match(self.match)
        
        self.assertTrue(result)
        self.assertTrue(PlayerMatchAggregate.objects.filter(
            player=self.player, match=self.match
        ).exists())
        self.assertTrue(MatchAggregate.objects.filter(
            match=self.match
        ).exists())