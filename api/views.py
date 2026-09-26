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
from django.utils import timezone
from rest_framework import permissions, status, throttling, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle as DRFUserRateThrottle

from aggregates.models import CoachMatchAggregate, MatchAggregate, PlayerMatchAggregate
from aggregates.services import countable_evaluations, min_votes_for_display, published_q, vote_weighted_avg
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


# Потолок ?limit= — иначе один запрос выгружает всю таблицу.
MAX_LIST_LIMIT = 100


def _parse_limit(request, default: int) -> int:
    """?limit= в пределах 1..MAX_LIST_LIMIT; мусор — default."""
    try:
        value = int(request.query_params.get("limit", default))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_LIST_LIMIT))


def _parse_uuid(value):
    """UUID из параметра запроса или None (вместо 500 на кривом id)."""
    import uuid

    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


class NoChangesAfterCompletionMixin:
    """Удаление оценки после завершения сессии запрещено, как и правка."""

    def perform_destroy(self, instance):
        from rest_framework.exceptions import ValidationError

        from evaluations.models import EvaluationSession

        if EvaluationSession.objects.filter(
            user=self.request.user, match_id=instance.match_id, status="completed",
        ).exists():
            raise ValidationError("Оценка этого матча уже завершена — изменить её нельзя")
        instance.delete()




# ============================================================================
# ContextEvaluationViewSet
# ============================================================================
class ContextEvaluationViewSet(NoChangesAfterCompletionMixin, viewsets.ModelViewSet):
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
class PlayerEvaluationViewSet(NoChangesAfterCompletionMixin, viewsets.ModelViewSet):
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
        match_uuid = _parse_uuid(match_id)
        match = Match.objects.filter(id=match_uuid).only("id", "voting_open_until").first() if match_uuid else None
        if match is None:
            return Response({"error": "match not found"}, status=status.HTTP_404_NOT_FOUND)
        # Пока голосование открыто, чужие голоса не показываем — иначе подстраиваются под них.
        if match.voting_open_until >= timezone.now():
            return Response({"error": "Оценки откроются после закрытия голосования"}, status=status.HTTP_403_FORBIDDEN)

        cache_key = f"player_evaluations_by_match_{match_uuid}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        evaluations = (
            countable_evaluations(PlayerEvaluation.objects.filter(match_id=match_uuid), match_uuid)
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
        if _parse_uuid(player_id) is None:
            return Response({"error": "invalid player_id"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"player_analytics_{player_id}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        aggregates = (
            PlayerMatchAggregate.objects.filter(published_q(), player_id=player_id)
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
        summary_data = PlayerMatchAggregate.objects.filter(published_q(), player_id=player_id).aggregate(
            # Алиас не total_votes — иначе конфликт с Sum('total_votes') в vote_weighted_avg.
            votes_sum=Sum("total_votes"),
            # Те же средние, что на сайте: с весом по числу голосов.
            avg_performance=vote_weighted_avg("performance_score"),
            avg_risk=vote_weighted_avg("risk_index"),
            avg_maturity=vote_weighted_avg("maturity_score"),
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
                "total_votes": summary_data["votes_sum"] or 0,
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
class TeamEvaluationViewSet(NoChangesAfterCompletionMixin, viewsets.ModelViewSet):
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
class CoachEvaluationViewSet(NoChangesAfterCompletionMixin, viewsets.ModelViewSet):
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
class RefereeEvaluationViewSet(NoChangesAfterCompletionMixin, viewsets.ModelViewSet):
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
class MatchEvaluationViewSet(NoChangesAfterCompletionMixin, viewsets.ModelViewSet):
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

        match_uuid = _parse_uuid(match_id)
        if match_uuid is None:
            return Response({"error": "invalid match_id"}, status=status.HTTP_400_BAD_REQUEST)
        match = get_object_or_404(
            Match.objects.select_related("home_team", "away_team"), id=match_uuid
        )
        # До закрытия голосования агрегат матча не публикуем.
        match_agg = MatchAggregate.objects.filter(published_q(), match=match).first()
        stats = countable_evaluations(MatchEvaluation.objects.filter(match=match), match.id).aggregate(
            total_match_evals=Count("id"),
            avg_entertainment=Avg("entertainment"),
            avg_tension=Avg("tension"),
        )
        player_evals_count = countable_evaluations(PlayerEvaluation.objects.filter(match=match), match.id).count()

        response_data = {
            "match": MatchSerializer(match).data,
            "aggregate": MatchAggregateSerializer(match_agg).data if match_agg else None,
            "stats": {
                "total_match_evaluations": stats["total_match_evals"] or 0,
                "total_player_evaluations": player_evals_count,
                # Средние — только после закрытия голосования.
                "avg_entertainment": round(stats["avg_entertainment"] or 0, 2) if match_agg else None,
                "avg_tension": round(stats["avg_tension"] or 0, 2) if match_agg else None,
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
            MatchAggregate.objects.filter(published_q()).select_related(*MATCH_DETAIL_SELECT_RELATED)
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
        limit = _parse_limit(request, 10)
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
        # Ниже порога голосов рейтинг и на сайте не показываем.
        return (
            PlayerMatchAggregate.objects.filter(published_q(), total_votes__gte=min_votes_for_display()).select_related(
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
        limit = _parse_limit(request, 10)
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
        limit = _parse_limit(request, 20)
        if not season_id:
            return Response({"error": "season_id required"}, status=status.HTTP_400_BAD_REQUEST)
        if _parse_uuid(season_id) is None:
            return Response({"error": "invalid season_id"}, status=status.HTTP_400_BAD_REQUEST)

        cache_key = f"player_aggregates_season_{season_id}_{limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        aggregates = (
            PlayerMatchAggregate.objects.filter(published_q(), match__season_id=season_id, total_votes__gte=min_votes_for_display())
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
            CoachMatchAggregate.objects.filter(published_q(), total_votes__gte=min_votes_for_display()).select_related(
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