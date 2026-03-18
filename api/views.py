# api/views.py
from rest_framework import viewsets, status, permissions, throttling
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Avg, Count
from django.shortcuts import get_object_or_404


from django.core.cache import cache
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator


from evaluations.models import (
    ContextEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    MatchEvaluation
)
from matches.models import Match
from aggregates.models import PlayerMatchAggregate, MatchAggregate

from .serializers import (
    ContextEvaluationSerializer,
    TeamEvaluationSerializer,
    PlayerEvaluationSerializer,
    CoachEvaluationSerializer,
    RefereeEvaluationSerializer,
    MatchEvaluationSerializer,
    PlayerMatchAggregateSerializer,
    MatchAggregateSerializer
)


class VotingOpenPermission(permissions.BasePermission):
    """Проверка: голосование открыто"""
    message = "Голосование для этого матча закрыто"
    
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if hasattr(obj, 'voting_open_until'):
            return timezone.now() <= obj.voting_open_until
        return True


class UserRateThrottle(throttling.UserRateThrottle):
    rate = '100/hour'


class BurstRateThrottle(throttling.BurstRateThrottle):
    rate = '20/minute'


class ContextEvaluationViewSet(viewsets.ModelViewSet):
    """ViewSet для контекста просмотра"""
    queryset = ContextEvaluation.objects.all()
    serializer_class = ContextEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle, BurstRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return ContextEvaluation.objects.filter(user=user).select_related(
            'match', 'supported_team'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class PlayerEvaluationViewSet(viewsets.ModelViewSet):
    """ViewSet для оценок игроков"""
    queryset = PlayerEvaluation.objects.all()
    serializer_class = PlayerEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle, BurstRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return PlayerEvaluation.objects.filter(user=user).select_related(
            'match', 'player', 'player__team', 'match__home_team', 'match__away_team'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def by_match(self, request):
        match_id = request.query_params.get('match_id')
        if not match_id:
            return Response({'error': 'match_id required'}, status=status.HTTP_400_BAD_REQUEST)
        
        evaluations = PlayerEvaluation.objects.filter(
            match_id=match_id
        ).select_related('player', 'user', 'player__team').order_by('-contribution')
        
        serializer = self.get_serializer(evaluations, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def analytics(self, request):
        """Аналитика по игроку за сезон"""
        player_id = request.query_params.get('player_id')
        if not player_id:
            return Response(
                {'error': 'player_id required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        aggregates = PlayerMatchAggregate.objects.filter(
            player_id=player_id
        ).select_related('match', 'match__league').order_by('-match__start_time')
        
        serializer = PlayerMatchAggregateSerializer(aggregates, many=True)
        
        # Сводная статистика
        total_votes = aggregates.aggregate(total=Count('total_votes'))['total'] or 0
        avg_performance = aggregates.aggregate(avg=Avg('performance_score'))['avg'] or 0
        
        return Response({
            'aggregates': serializer.data,
            'summary': {
                'total_matches': aggregates.count(),
                'total_votes': total_votes,
                'avg_performance_score': round(avg_performance, 2)
            }
        })


class TeamEvaluationViewSet(viewsets.ModelViewSet):
    """ViewSet для оценок команд"""
    queryset = TeamEvaluation.objects.all()
    serializer_class = TeamEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle, BurstRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return TeamEvaluation.objects.filter(user=user).select_related(
            'match', 'team'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class CoachEvaluationViewSet(viewsets.ModelViewSet):
    """ViewSet для оценок тренеров"""
    queryset = CoachEvaluation.objects.all()
    serializer_class = CoachEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle, BurstRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return CoachEvaluation.objects.filter(user=user).select_related(
            'match', 'coach'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class RefereeEvaluationViewSet(viewsets.ModelViewSet):
    """ViewSet для оценок судейства"""
    queryset = RefereeEvaluation.objects.all()
    serializer_class = RefereeEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle, BurstRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return RefereeEvaluation.objects.filter(user=user).select_related('match')
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class MatchEvaluationViewSet(viewsets.ModelViewSet):
    """ViewSet для общих оценок матча"""
    queryset = MatchEvaluation.objects.all()
    serializer_class = MatchEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle, BurstRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return MatchEvaluation.objects.filter(user=user).select_related('match')
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Сводка по матчу"""
        match_id = request.query_params.get('match_id')
        if not match_id:
            return Response(
                {'error': 'match_id required'}, 
                status=status.HTTP_400_BAD_REQUEST
            )
        
        match = get_object_or_404(Match, id=match_id)
        
        # Агрегаты
        match_agg = MatchAggregate.objects.filter(match=match).first()
        
        # Количество оценок
        total_evals = MatchEvaluation.objects.filter(match=match).count()
        player_evals = PlayerEvaluation.objects.filter(match=match).count()
        
        return Response({
            'match': MatchEvaluationSerializer(match).data,
            'aggregate': MatchAggregateSerializer(match_agg).data if match_agg else None,
            'stats': {
                'total_match_evaluations': total_evals,
                'total_player_evaluations': player_evals
            }
        })


class MatchAggregateViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet для агрегатов матча (только чтение)"""
    queryset = MatchAggregate.objects.all()
    serializer_class = MatchAggregateSerializer
    permission_classes = [permissions.AllowAny]
    
    def get_queryset(self):
        return MatchAggregate.objects.select_related('match').order_by('-match__start_time')


class PlayerAggregateViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet для агрегатов игроков (только чтение)"""
    queryset = PlayerMatchAggregate.objects.all()
    serializer_class = PlayerMatchAggregateSerializer
    permission_classes = [permissions.AllowAny]
    
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f'player_aggregate_{player_id}_{match_id}'
        
        # Проверка кэша
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        
        # Если нет в кэше - сериализуем и сохраняем
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)  # 10 минут
        
        return Response(serializer.data)
    
    def get_queryset(self):
        return PlayerMatchAggregate.objects.select_related(
            'player', 'match', 'player__team', 'match__league', 'match__season'
        ).order_by('-performance_score')
    
    @action(detail=False, methods=['get'])
    def top_players(self, request):
        limit = int(request.query_params.get('limit', 10))
        cache_key = f'top_players_{limit}'
        
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        
        top_players = self.get_queryset()[:limit]
        serializer = self.get_serializer(top_players, many=True)
        
        cache.set(cache_key, serializer.data, timeout=300)
        
        return Response(serializer.data)