# api/views.py
"""DRF ViewSets.

.only() перечисляет и поля связанных моделей — иначе select_related не спасает от N+1.
Write-эндпоинты — IsAuthenticatedAndVerified.
"""
from __future__ import annotations

import logging

from django.core.cache import cache
from django.db.models import Avg, Count, Max, Prefetch, Sum
from django.shortcuts import get_object_or_404
from rest_framework import permissions, status, throttling, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle as DRFUserRateThrottle

from aggregates.models import CoachMatchAggregate, MatchAggregate, PlayerMatchAggregate
from evaluations.models import (
    CoachEvaluation,
    ContextEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from matches.models import Match

from .permissions import IsAuthenticatedAndVerified, VotingOpenPermission
from .serializers import (
    CoachEvaluationSerializer,
    CoachMatchAggregateSerializer,
    ContextEvaluationSerializer,
    MatchAggregateSerializer,
    MatchEvaluationSerializer,
    MatchSerializer,
    PlayerEvaluationSerializer,
    PlayerMatchAggregateSerializer,
    RefereeEvaluationSerializer,
    TeamEvaluationSerializer,
)

logger = logging.getLogger(__name__)

# Поля Match для MatchSerializer — держать в синхроне с .only().
MATCH_DETAIL_ONLY_FIELDS = (
    "match__start_time",
    "match__voting_open_until",
    "match__home_score",
    "match__away_score",
    "match__status",
    "match__home_team__name",
    "match__away_team__name",
)
MATCH_DETAIL_SELECT_RELATED = ("match", "match__home_team", "match__away_team")


class EvaluationRateThrottle(DRFUserRateThrottle):
    rate = "20/minute"


class AggregateRateThrottle(AnonRateThrottle):
    rate = "100/hour"


class StandardUserRateThrottle(throttling.UserRateThrottle):
    """Не называть UserRateThrottle — затрёт импорт из DRF."""

    rate = "100/hour"




# ============================================================================
# ContextEvaluationViewSet
# ============================================================================
class ContextEvaluationViewSet(viewsets.ModelViewSet):
    queryset = ContextEvaluation.objects.all()
    serializer_class = ContextEvaluationSerializer
    permission_classes = [IsAuthenticatedAndVerified, VotingOpenPermission]
    throttle_classes = [StandardUserRateThrottle]

    def get_queryset(self):
        user = self.request.user
        return (
            ContextEvaluation.objects.filter(user=user)
            .select_related(*MATCH_DETAIL_SELECT_RELATED, "supported_team")
            .only(
                "id",
                "user_id",
                "match_id",
                "supported_team_id",
                "supported_team__name",
                "watched_type",
                "attended_stadium",
                "created_at",
                "updated_at",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
        cache.delete(f"context_eval_{self.request.user.id}")


# ============================================================================
# PlayerEvaluationViewSet
# ============================================================================
class PlayerEvaluationViewSet(viewsets.ModelViewSet):
    queryset = PlayerEvaluation.objects.all()
    serializer_class = PlayerEvaluationSerializer
    permission_classes = [IsAuthenticatedAndVerified, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]

    def get_queryset(self):
        # Без select_related("user") — user не сериализуется, а с .only() JOIN падает.
        user = self.request.user
        return (
            PlayerEvaluation.objects.filter(user=user)
            .select_related("player", *MATCH_DETAIL_SELECT_RELATED)
            .only(
                "id",
                "user_id",
                "match_id",
                "player_id",
                "player__first_name",
                "player__last_name",
                "player__number",
                "contribution",
                "risk",
                "potential",
                "created_at",
                "updated_at",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f"player_aggregate_{instance.player_id}_{instance.match_id}")
        cache.delete(f"match_player_aggregates_{instance.match_id}")

    @action(detail=False, methods=["get"])
    def by_match(self, request):
        match_id = request.query_params.get("match_id")
        if not match_id:
            return Response({"error": "match_id required"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"player_evaluations_by_match_{match_id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        evaluations = (
            PlayerEvaluation.objects.filter(match_id=match_id)
            # Без select_related("user") — см. get_queryset().
            .select_related("player", *MATCH_DETAIL_SELECT_RELATED)
            .order_by("-contribution")
            .only(
                "id",
                "player_id",
                "player__first_name",
                "player__last_name",
                "player__number",
                "contribution",
                "risk",
                "potential",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )
        serializer = self.get_serializer(evaluations, many=True)
        cache.set(cache_key, serializer.data, timeout=300)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def analytics(self, request):
        player_id = request.query_params.get("player_id")
        if not player_id:
            return Response({"error": "player_id required"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"player_analytics_{player_id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        aggregates = (
            PlayerMatchAggregate.objects.filter(player_id=player_id)
            .select_related(*MATCH_DETAIL_SELECT_RELATED)
            .order_by("-match__start_time")
            .only(
                "id",
                "player_id",
                "match_id",
                "avg_contribution",
                "avg_risk",
                "avg_potential",
                "total_votes",
                "performance_score",
                "risk_index",
                "maturity_score",
                "stability_index",
                "clutch_index",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )
        # Sum, а не Count — нужна сумма голосов.
        summary_data = PlayerMatchAggregate.objects.filter(player_id=player_id).aggregate(
            total_votes=Sum("total_votes"),
            avg_performance=Avg("performance_score"),
            avg_risk=Avg("risk_index"),
            avg_maturity=Avg("maturity_score"),
            max_clutch=Max("clutch_index"),
            matches_count=Count("id"),
        )
        serializer = PlayerMatchAggregateSerializer(
            aggregates, many=True, context={"request": request}
        )
        response_data = {
            "aggregates": serializer.data,
            "summary": {
                "total_matches": summary_data["matches_count"] or 0,
                "total_votes": summary_data["total_votes"] or 0,
                "avg_performance_score": round(summary_data["avg_performance"] or 0, 2),
                "avg_risk_index": round(summary_data["avg_risk"] or 0, 2),
                "avg_maturity_score": round(summary_data["avg_maturity"] or 0, 2),
                "max_clutch_index": round(summary_data["max_clutch"] or 0, 2),
            },
        }
        cache.set(cache_key, response_data, timeout=600)
        return Response(response_data)


# ============================================================================
# TeamEvaluationViewSet
# ============================================================================
class TeamEvaluationViewSet(viewsets.ModelViewSet):
    queryset = TeamEvaluation.objects.all()
    serializer_class = TeamEvaluationSerializer
    permission_classes = [IsAuthenticatedAndVerified, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]

    def get_queryset(self):
        user = self.request.user
        return (
            TeamEvaluation.objects.filter(user=user)
            .select_related(*MATCH_DETAIL_SELECT_RELATED, "team")
            .only(
                "id",
                "user_id",
                "match_id",
                "team_id",
                "team__name",
                "tactics",
                "effort",
                "organization",
                "mentality",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f"team_aggregate_{instance.team_id}_{instance.match_id}")


# ============================================================================
# CoachEvaluationViewSet
# ============================================================================
class CoachEvaluationViewSet(viewsets.ModelViewSet):
    queryset = CoachEvaluation.objects.all()
    serializer_class = CoachEvaluationSerializer
    permission_classes = [IsAuthenticatedAndVerified, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]

    def get_queryset(self):
        user = self.request.user
        return (
            CoachEvaluation.objects.filter(user=user)
            .select_related("coach", *MATCH_DETAIL_SELECT_RELATED)
            .only(
                "id",
                "user_id",
                "match_id",
                "coach_id",
                "coach__first_name",
                "coach__last_name",
                "tactics",
                "substitutions",
                "game_management",
                "impact",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f"coach_aggregate_{instance.coach_id}_{instance.match_id}")


# ============================================================================
# RefereeEvaluationViewSet
# ============================================================================
class RefereeEvaluationViewSet(viewsets.ModelViewSet):
    queryset = RefereeEvaluation.objects.all()
    serializer_class = RefereeEvaluationSerializer
    permission_classes = [IsAuthenticatedAndVerified, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]

    def get_queryset(self):
        user = self.request.user
        return (
            RefereeEvaluation.objects.filter(user=user)
            .select_related(*MATCH_DETAIL_SELECT_RELATED)
            .only(
                "id",
                "user_id",
                "match_id",
                "influence_score",
                "decision_quality",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


# ============================================================================
# MatchEvaluationViewSet
# ============================================================================
class MatchEvaluationViewSet(viewsets.ModelViewSet):
    queryset = MatchEvaluation.objects.all()
    serializer_class = MatchEvaluationSerializer
    permission_classes = [IsAuthenticatedAndVerified, VotingOpenPermission]
    throttle_classes = [EvaluationRateThrottle]

    def get_queryset(self):
        user = self.request.user
        return (
            MatchEvaluation.objects.filter(user=user)
            .select_related(*MATCH_DETAIL_SELECT_RELATED)
            .only(
                "id",
                "user_id",
                "match_id",
                "entertainment",
                "tension",
                "turning_point",
                "fairness",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def perform_create(self, serializer):
        instance = serializer.save(user=self.request.user)
        cache.delete(f"match_aggregate_{instance.match_id}")
        cache.delete(f"match_evaluations_{instance.match_id}")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        match_id = request.query_params.get("match_id")
        if not match_id:
            return Response({"error": "match_id required"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"match_summary_{match_id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        match = get_object_or_404(
            Match.objects.select_related("home_team", "away_team"), id=match_id
        )
        match_agg = MatchAggregate.objects.filter(match=match).first()
        stats = MatchEvaluation.objects.filter(match=match).aggregate(
            total_match_evals=Count("id"),
            avg_entertainment=Avg("entertainment"),
            avg_tension=Avg("tension"),
        )
        player_evals_count = PlayerEvaluation.objects.filter(match=match).count()

        response_data = {
            "match": MatchSerializer(match).data,
            "aggregate": MatchAggregateSerializer(match_agg).data if match_agg else None,
            "stats": {
                "total_match_evaluations": stats["total_match_evals"] or 0,
                "total_player_evaluations": player_evals_count,
                "avg_entertainment": round(stats["avg_entertainment"] or 0, 2),
                "avg_tension": round(stats["avg_tension"] or 0, 2),
            },
        }
        cache.set(cache_key, response_data, timeout=300)
        return Response(response_data)


# ============================================================================
# MatchAggregateViewSet
# ============================================================================
class MatchAggregateViewSet(viewsets.ReadOnlyModelViewSet):
    """Агрегаты матча с кэшированием."""

    queryset = MatchAggregate.objects.all()
    serializer_class = MatchAggregateSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [AggregateRateThrottle]

    def get_queryset(self):
        """Без среза внутри Prefetch — он применяется ко всему набору, а не к каждому матчу."""
        return (
            MatchAggregate.objects.select_related(*MATCH_DETAIL_SELECT_RELATED)
            .prefetch_related(
                Prefetch(
                    "match__player_aggregates",
                    queryset=PlayerMatchAggregate.objects.select_related(
                        "player", "player__team"
                    ).order_by("-performance_score"),
                )
            )
            .order_by("-match__start_time")
            .only(
                "id",
                "match_id",
                "avg_entertainment",
                "avg_tension",
                "avg_fairness",
                "drama_index",
                "total_votes",
                "created_at",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f"match_aggregate_{instance.id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def recent(self, request):
        """Срез внешнего queryset безопасен для prefetch."""
        limit = int(request.query_params.get("limit", 10))
        cache_key = f"recent_match_aggregates_{limit}"
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
        # Без select_related("player__team") — команда не сериализуется.
        return (
            PlayerMatchAggregate.objects.select_related(
                "player", *MATCH_DETAIL_SELECT_RELATED
            )
            .order_by("-performance_score")
            .only(
                "id",
                "player_id",
                "player__first_name",
                "player__last_name",
                "match_id",
                "avg_contribution",
                "avg_risk",
                "avg_potential",
                "performance_score",
                "risk_index",
                "maturity_score",
                "stability_index",
                "clutch_index",
                "total_votes",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f"player_aggregate_{instance.player_id}_{instance.match_id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def top_players(self, request):
        limit = int(request.query_params.get("limit", 10))
        cache_key = f"top_players_{limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        top_players = self.get_queryset()[:limit]
        serializer = self.get_serializer(top_players, many=True)
        cache.set(cache_key, serializer.data, timeout=300)
        return Response(serializer.data)

    @action(detail=False, methods=["get"])
    def by_season(self, request):
        season_id = request.query_params.get("season_id")
        limit = int(request.query_params.get("limit", 20))
        if not season_id:
            return Response({"error": "season_id required"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"player_aggregates_season_{season_id}_{limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        aggregates = (
            PlayerMatchAggregate.objects.filter(match__season_id=season_id)
            # Без select_related("player__team").
            .select_related("player", *MATCH_DETAIL_SELECT_RELATED)
            .order_by("-performance_score")[:limit]
        )
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
        # Без select_related("coach__team") — команда не сериализуется.
        return (
            CoachMatchAggregate.objects.select_related(
                "coach", *MATCH_DETAIL_SELECT_RELATED
            )
            .order_by("-match__start_time")
            .only(
                "id",
                "coach_id",
                "coach__first_name",
                "coach__last_name",
                "match_id",
                "avg_tactics",
                "avg_substitutions",
                "avg_management",
                "avg_impact",
                "total_votes",
                *MATCH_DETAIL_ONLY_FIELDS,
            )
        )

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        cache_key = f"coach_aggregate_{instance.coach_id}_{instance.match_id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)
        serializer = self.get_serializer(instance)
        cache.set(cache_key, serializer.data, timeout=600)
        return Response(serializer.data)