from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone
from datetime import timedelta

from matches.models import Match
from teams.models import Team
from players.models import Player
from seasons.models import Season
from leagues.models import League

from .models import (
    ContextEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    MatchEvaluation
)

User = get_user_model()


class EvaluationUniqueConstraintTests(TestCase):
    """Тесты на уникальность оценок"""
    
    def setUp(self):
        # Создаём тестовые данные
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.league = League.objects.create(
            name='Test League',
            country='Kazakhstan'
        )
        
        self.season = Season.objects.create(
            league=self.league,
            year='2026',
            is_active=True
        )
        
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
            last_name='Player'
        )
    
    def test_context_evaluation_unique(self):
        """Один пользователь может оценить контекст матча только один раз"""
        ContextEvaluation.objects.create(
            user=self.user,
            match=self.match,
            watched_type='full'
        )
        
        with self.assertRaises(IntegrityError):
            ContextEvaluation.objects.create(
                user=self.user,
                match=self.match,
                watched_type='highlights'
            )
    
    def test_player_evaluation_unique(self):
        """Один пользователь может оценить игрока в матче только один раз"""
        PlayerEvaluation.objects.create(
            user=self.user,
            match=self.match,
            player=self.player,
            contribution=8,
            risk=3,
            potential=7
        )
        
        with self.assertRaises(IntegrityError):
            PlayerEvaluation.objects.create(
                user=self.user,
                match=self.match,
                player=self.player,
                contribution=5,
                risk=5,
                potential=5
            )
    
    def test_team_evaluation_unique(self):
        """Один пользователь может оценить команду в матче только один раз"""
        TeamEvaluation.objects.create(
            user=self.user,
            match=self.match,
            team=self.team1,
            tactics=8,
            effort=7,
            organization=6,
            mentality=7
        )
        
        with self.assertRaises(IntegrityError):
            TeamEvaluation.objects.create(
                user=self.user,
                match=self.match,
                team=self.team1,
                tactics=5,
                effort=5,
                organization=5,
                mentality=5
            )
    
    def test_match_evaluation_unique(self):
        """Один пользователь может оценить матч только один раз"""
        MatchEvaluation.objects.create(
            user=self.user,
            match=self.match,
            entertainment=8,
            tension=7,
            fairness=8
        )
        
        with self.assertRaises(IntegrityError):
            MatchEvaluation.objects.create(
                user=self.user,
                match=self.match,
                entertainment=5,
                tension=5,
                fairness=5
            )


class EvaluationValidationTests(TestCase):
    """Тесты на валидацию оценок"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.league = League.objects.create(name='Test League', country='Kazakhstan')
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
        
        self.player = Player.objects.create(first_name='Test', last_name='Player')
    
    def test_player_evaluation_min_max_values(self):
        """Проверка валидации мин/макс значений"""
        # Должно работать (в пределах 1-10)
        eval_valid = PlayerEvaluation(
            user=self.user,
            match=self.match,
            player=self.player,
            contribution=1,
            risk=10,
            potential=5
        )
        eval_valid.full_clean()  # Не должно выбросить исключение
        
        # Должно выбросить исключение (< 1)
        eval_invalid = PlayerEvaluation(
            user=self.user,
            match=self.match,
            player=self.player,
            contribution=0,  # < 1
            risk=5,
            potential=5
        )
        with self.assertRaises(ValidationError):
            eval_invalid.full_clean()
        
        # Должно выбросить исключение (> 10)
        eval_invalid2 = PlayerEvaluation(
            user=self.user,
            match=self.match,
            player=self.player,
            contribution=11,  # > 10
            risk=5,
            potential=5
        )
        with self.assertRaises(ValidationError):
            eval_invalid2.full_clean()
    
    def test_referee_evaluation_influence_score_range(self):
        """Проверка валидации influence_score (0-100)"""
        from coaches.models import Coach
        self.coach = Coach.objects.create(first_name='Test', last_name='Coach')
        
        # Должно работать (0-100)
        eval_valid = RefereeEvaluation(
            user=self.user,
            match=self.match,
            influence_score=0,
            decision_quality=5
        )
        eval_valid.full_clean()
        
        eval_valid2 = RefereeEvaluation(
            user=self.user,
            match=self.match,
            influence_score=100,
            decision_quality=5
        )
        eval_valid2.full_clean()
        
        # Должно выбросить исключение
        eval_invalid = RefereeEvaluation(
            user=self.user,
            match=self.match,
            influence_score=101,  # > 100
            decision_quality=5
        )
        with self.assertRaises(ValidationError):
            eval_invalid.full_clean()


class EvaluationPropertyTests(TestCase):
    """Тесты на вычисляемые свойства"""
    
    def setUp(self):
        self.user = User.objects.create_user(
            username='testuser',
            email='test@example.com',
            password='testpass123'
        )
        
        self.league = League.objects.create(name='Test League', country='Kazakhstan')
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
        
        self.player = Player.objects.create(first_name='Test', last_name='Player')
    
    def test_player_evaluation_maturity_score(self):
        """Maturity Score = contribution - risk"""
        evaluation = PlayerEvaluation.objects.create(
            user=self.user,
            match=self.match,
            player=self.player,
            contribution=8,
            risk=3,
            potential=7
        )
        
        self.assertEqual(evaluation.maturity_score, 5)  # 8 - 3 = 5
    
    def test_team_evaluation_average_score(self):
        """Средний балл команды"""
        evaluation = TeamEvaluation.objects.create(
            user=self.user,
            match=self.match,
            team=self.team1,
            tactics=8,
            effort=7,
            organization=6,
            mentality=7
        )
        
        self.assertEqual(evaluation.average_score, 7.0)  # (8+7+6+7)/4 = 7.0
    
    def test_match_evaluation_drama_index(self):
        """Drama Index = entertainment * tension"""
        evaluation = MatchEvaluation.objects.create(
            user=self.user,
            match=self.match,
            entertainment=8,
            tension=9,
            fairness=7
        )
        
        self.assertEqual(evaluation.drama_index, 72)  # 8 * 9 = 72