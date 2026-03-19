# api/views.py
from rest_framework import viewsets, status, permissions, throttling
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from django.db.models import Avg, Count, F, Prefetch, Q, Max
from django.shortcuts import get_object_or_404
from django.core.cache import cache
from django.views.decorators.cache import cache_page
from django.utils.decorators import method_decorator
from django.db import connection
import logging
import time
from evaluations.models import (
    ContextEvaluation,
    TeamEvaluation,
    PlayerEvaluation,
    CoachEvaluation,
    RefereeEvaluation,
    MatchEvaluation
)
from matches.models import Match
from rest_framework.throttling import UserRateThrottle, AnonRateThrottle
from aggregates.models import PlayerMatchAggregate, MatchAggregate, CoachMatchAggregate
from .serializers import (
    ContextEvaluationSerializer,
    TeamEvaluationSerializer,
    PlayerEvaluationSerializer,
    CoachEvaluationSerializer,
    RefereeEvaluationSerializer,
    MatchEvaluationSerializer,
    PlayerMatchAggregateSerializer,
    MatchAggregateSerializer,
    CoachMatchAggregateSerializer
)

logger = logging.getLogger(__name__)


class EvaluationRateThrottle(UserRateThrottle):
    rate = '20/minute'

class AggregateRateThrottle(AnonRateThrottle):
    rate = '100/hour'


class VotingOpenPermission(permissions.BasePermission):
    """Проверка: голосование открыто"""
    message = "Голосование для этого матча закрыто"
    
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        if hasattr(obj, 'voting_open_until'):
            return timezone.now() <= obj.voting_open_until
        if hasattr(obj, 'match') and hasattr(obj.match, 'voting_open_until'):
            return timezone.now() <= obj.match.voting_open_until
        return True

class UserRateThrottle(throttling.UserRateThrottle):
    rate = '100/hour'

# ============================================================================
# ContextEvaluationViewSet
# ============================================================================
class ContextEvaluationViewSet(viewsets.ModelViewSet):
    queryset = ContextEvaluation.objects.all()
    serializer_class = ContextEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [UserRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return ContextEvaluation.objects.filter(
            user=user
        ).select_related(
            'match',
            'match__home_team',
            'match__away_team',
            'supported_team'
        ).only(
            'id', 'user_id', 'match_id', 'supported_team_id',
            'watched_type', 'attended_stadium', 'created_at', 'updated_at'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
        cache.delete(f'context_eval_{self.request.user.id}')

# ============================================================================
# PlayerEvaluationViewSet
# ============================================================================
class PlayerEvaluationViewSet(viewsets.ModelViewSet):
    queryset = PlayerEvaluation.objects.all()
    serializer_class = PlayerEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return PlayerEvaluation.objects.filter(
            user=user
        ).select_related(
            'match',
            'player',
            'player__team',
            'user'
        ).only(
            'id', 'user_id', 'match_id', 'player_id',
            'contribution', 'risk', 'potential', 'created_at', 'updated_at'
        )
    
    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f'player_aggregate_{instance.player_id}_{instance.match_id}')
        cache.delete(f'match_player_aggregates_{instance.match_id}')
    
    @action(detail=False, methods=['get'])
    def by_match(self, request):
        match_id = request.query_params.get('match_id')
        if not match_id:
            return Response(
                {'error': 'match_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        cache_key = f'player_evaluations_by_match_{match_id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        evaluations = PlayerEvaluation.objects.filter(
            match_id=match_id
        ).select_related(
            'player',
            'player__team',
            'user'
        ).order_by('-contribution').only(
            'id', 'player_id', 'contribution', 'risk', 'potential'
        )
        serializer = self.get_serializer(evaluations, many=True)
        cache.set(cache_key, serializer.data, timeout=300)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def analytics(self, request):
        player_id = request.query_params.get('player_id')
        if not player_id:
            return Response(
                {'error': 'player_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        cache_key = f'player_analytics_{player_id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        aggregates = PlayerMatchAggregate.objects.filter(
            player_id=player_id
        ).select_related(
            'match',
            'match__league',
            'match__season'
        ).order_by('-match__start_time')
        summary_data = PlayerMatchAggregate.objects.filter(
            player_id=player_id
        ).aggregate(
            total_votes=Count('total_votes'),
            avg_performance=Avg('performance_score'),
            avg_risk=Avg('risk_index'),
            avg_maturity=Avg('maturity_score'),
            max_clutch=Max('clutch_index'),
            matches_count=Count('id')
        )
        serializer = PlayerMatchAggregateSerializer(aggregates, many=True)
        response_data = {
            'aggregates': serializer.data,
            'summary': {
                'total_matches': summary_data['matches_count'] or 0,
                'total_votes': summary_data['total_votes'] or 0,
                'avg_performance_score': round(summary_data['avg_performance'] or 0, 2),
                'avg_risk_index': round(summary_data['avg_risk'] or 0, 2),
                'avg_maturity_score': round(summary_data['avg_maturity'] or 0, 2),
                'max_clutch_index': round(summary_data['max_clutch'] or 0, 2),
            }
        }
        cache.set(cache_key, response_data, timeout=600)
        return Response(response_data)

# ============================================================================
# TeamEvaluationViewSet
# ============================================================================
class TeamEvaluationViewSet(viewsets.ModelViewSet):
    queryset = TeamEvaluation.objects.all()
    serializer_class = TeamEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return TeamEvaluation.objects.filter(
            user=user
        ).select_related(
            'match',
            'team'
        ).only(
            'id', 'user_id', 'match_id', 'team_id',
            'tactics', 'effort', 'organization', 'mentality'
        )
    
    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f'team_aggregate_{instance.team_id}_{instance.match_id}')

# ============================================================================
# CoachEvaluationViewSet
# ============================================================================
class CoachEvaluationViewSet(viewsets.ModelViewSet):
    queryset = CoachEvaluation.objects.all()
    serializer_class = CoachEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return CoachEvaluation.objects.filter(
            user=user
        ).select_related(
            'match',
            'coach',
            'coach__team'
        ).only(
            'id', 'user_id', 'match_id', 'coach_id',
            'tactics', 'substitutions', 'game_management', 'impact'
        )
    
    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f'coach_aggregate_{instance.coach_id}_{instance.match_id}')

# ============================================================================
# RefereeEvaluationViewSet
# ============================================================================
class RefereeEvaluationViewSet(viewsets.ModelViewSet):
    queryset = RefereeEvaluation.objects.all()
    serializer_class = RefereeEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return RefereeEvaluation.objects.filter(
            user=user
        ).select_related('match').only(
            'id', 'user_id', 'match_id',
            'influence_score', 'decision_quality'
        )
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

# ============================================================================
# MatchEvaluationViewSet
# ============================================================================
class MatchEvaluationViewSet(viewsets.ModelViewSet):
    queryset = MatchEvaluation.objects.all()
    serializer_class = MatchEvaluationSerializer
    permission_classes = [permissions.IsAuthenticated, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]
    
    def get_queryset(self):
        user = self.request.user
        return MatchEvaluation.objects.filter(
            user=user
        ).select_related('match').only(
            'id', 'user_id', 'match_id',
            'entertainment', 'tension', 'turning_point', 'fairness'
        )
    
    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f'match_aggregate_{instance.match_id}')
        cache.delete(f'match_evaluations_{instance.match_id}')
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        match_id = request.query_params.get('match_id')
        if not match_id:
            return Response(
                {'error': 'match_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        cache_key = f'match_summary_{match_id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        match = get_object_or_404(Match, id=match_id)
        match_agg = MatchAggregate.objects.filter(match=match).first()
        stats = MatchEvaluation.objects.filter(match=match).aggregate(
            total_match_evals=Count('id'),
            avg_entertainment=Avg('entertainment'),
            avg_tension=Avg('tension')
        )
        player_evals_count = PlayerEvaluation.objects.filter(
            match=match
        ).count()
        response_data = {
            'match': MatchEvaluationSerializer(match).data,
            'aggregate': MatchAggregateSerializer(match_agg).data if match_agg else None,
            'stats': {
                'total_match_evaluations': stats['total_match_evals'] or 0,
                'total_player_evaluations': player_evals_count,
                'avg_entertainment': round(stats['avg_entertainment'] or 0, 2),
                'avg_tension': round(stats['avg_tension'] or 0, 2),
            }
        }
        cache.set(cache_key, response_data, timeout=300)
        return Response(response_data)

# ============================================================================
# MatchAggregateViewSet — ✅ ИСПРАВЛЕННЫЙ
# ============================================================================
class MatchAggregateViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet для агрегатов матча — с полным кэшированием"""
    queryset = MatchAggregate.objects.all()
    serializer_class = MatchAggregateSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AggregateRateThrottle]
    def get_queryset(self):
        """
        ✅ FIX: Убрали срез [:11] из Prefetch
        """
        return MatchAggregate.objects.select_related(
            'match',
            'match__home_team',
            'match__away_team',
            'match__league',
            'match__season',
            'match__stadium'
        ).prefetch_related(
            Prefetch(
                'match__player_aggregates',
                queryset=PlayerMatchAggregate.objects.select_related(
                    'player', 'player__team'
                ).order_by('-performance_score')
            )
        ).order_by('-match__start_time').only(
            'id', 'match_id', 'avg_entertainment', 'avg_tension',
            'avg_fairness', 'drama_index', 'total_votes', 'created_at'
        )
    
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f'match_aggregate_{instance.id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def recent(self, request):
        limit = int(request.query_params.get('limit', 10))
        cache_key = f'recent_match_aggregates_{limit}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        aggregates = self.get_queryset()[:limit]
        serializer = self.get_serializer(aggregates, many=True)
        cache.set(cache_key, serializer.data, timeout=300)
        return Response(serializer.data)

# ============================================================================
# PlayerAggregateViewSet
# ============================================================================
class PlayerAggregateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = PlayerMatchAggregate.objects.all()
    serializer_class = PlayerMatchAggregateSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AggregateRateThrottle]
    
    def get_queryset(self):
        return PlayerMatchAggregate.objects.select_related(
            'player',
            'player__team',
            'match',
            'match__league',
            'match__season'
        ).order_by('-performance_score').only(
            'id', 'player_id', 'match_id',
            'performance_score', 'risk_index', 'maturity_score',
            'stability_index', 'clutch_index', 'total_votes'
        )
    
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f'player_aggregate_{instance.player_id}_{instance.match_id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)
    
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
    
    @action(detail=False, methods=['get'])
    def by_season(self, request):
        season_id = request.query_params.get('season_id')
        limit = int(request.query_params.get('limit', 20))
        if not season_id:
            return Response(
                {'error': 'season_id required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        cache_key = f'player_aggregates_season_{season_id}_{limit}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        aggregates = PlayerMatchAggregate.objects.filter(
            match__season_id=season_id
        ).select_related(
            'player',
            'player__team',
            'match'
        ).order_by('-performance_score')[:limit]
        serializer = self.get_serializer(aggregates, many=True)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)

# ============================================================================
# CoachAggregateViewSet
# ============================================================================
class CoachAggregateViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = CoachMatchAggregate.objects.all()
    serializer_class = CoachMatchAggregateSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AggregateRateThrottle]

    def get_queryset(self):
        return CoachMatchAggregate.objects.select_related(
            'coach',
            'coach__team',
            'match'
        ).order_by('-match__start_time').only(
            'id', 'coach_id', 'match_id',
            'avg_tactics', 'avg_substitutions',
            'avg_management', 'avg_impact', 'total_votes'
        )
    
    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f'coach_aggregate_{instance.coach_id}_{instance.match_id}'
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)
