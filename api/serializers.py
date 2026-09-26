# api/serializers.py
"""DRF-сериалайзеры. read_only_fields — tuple, не list (общий мутабельный атрибут класса)."""
from __future__ import annotations

from rest_framework import serializers

from aggregates.models import CoachMatchAggregate, MatchAggregate, PlayerMatchAggregate
from evaluations.models import (
    CoachEvaluation,
    ContextEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from evaluations.policies import (
    EvaluationPolicyError,
    assert_coach_in_match,
    assert_context_exists,
    assert_player_in_squad,
    assert_team_in_match,
    assert_voting_open,
)
from matches.models import Match


def _run_policy(check, *args) -> None:
    """Проверка из evaluations/policies.py -> serializers.ValidationError."""
    try:
        check(*args)
    except EvaluationPolicyError as e:
        raise serializers.ValidationError(str(e)) from e


def _forbid_after_completion(user, match) -> None:
    """Завершённую оценку матча не меняем ни через сайт, ни через API."""
    from evaluations.models import EvaluationSession

    if user and match and EvaluationSession.objects.filter(user=user, match=match, status="completed").exists():
        raise serializers.ValidationError("Оценка этого матча уже завершена — изменить её нельзя")


def _forbid_identity_field_changes(instance, data: dict, field_names: tuple[str, ...]) -> None:
    """При PATCH/PUT нельзя менять поля, определяющие оценку (match/player/team/coach/supported_team) —
    меняются только баллы.
    """
    if instance is None:
        return
    for field_name in field_names:
        if field_name not in data:
            continue
        new_value = data[field_name]
        new_id = new_value.id if new_value is not None else None
        current_id = getattr(instance, f"{field_name}_id")
        if new_id != current_id:
            raise serializers.ValidationError(
                {field_name: "Нельзя изменить после создания оценки — только баллы."}
            )


class MatchSerializer(serializers.ModelSerializer):
    """Матч для вложенных данных."""

    home_team_name = serializers.CharField(source="home_team.name", read_only=True)
    away_team_name = serializers.CharField(source="away_team.name", read_only=True)
    home_score = serializers.IntegerField(read_only=True)
    away_score = serializers.IntegerField(read_only=True)
    status = serializers.CharField(read_only=True)

    class Meta:
        model = Match
        fields = (
            "id",
            "home_team_name",
            "away_team_name",
            "home_score",
            "away_score",
            "status",
            "start_time",
            "voting_open_until",
        )


class ContextEvaluationSerializer(serializers.ModelSerializer):
    """Контекст просмотра матча."""

    match_details = MatchSerializer(source="match", read_only=True)
    supported_team_name = serializers.CharField(
        source="supported_team.name", read_only=True, allow_null=True
    )

    class Meta:
        model = ContextEvaluation
        fields = (
            "id",
            "match",
            "match_details",
            "supported_team",
            "supported_team_name",
            "watched_type",
            "attended_stadium",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_match(self, value: Match) -> Match:
        _run_policy(assert_voting_open, value)
        return value

    def validate(self, data: dict) -> dict:
        """Уникальность user + match и supported_team из этого матча."""
        _forbid_identity_field_changes(self.instance, data, ("match", "supported_team"))
        request = self.context.get("request")
        _forbid_after_completion(
            request.user if request else None,
            data.get("match") or (self.instance.match if self.instance else None),
        )

        request = self.context.get("request")
        user = request.user if request else None
        match = data.get("match")
        if user and match:
            existing = ContextEvaluation.objects.filter(user=user, match=match).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили контекст этого матча")

            supported_team = data.get("supported_team")
            if supported_team is not None:
                _run_policy(assert_team_in_match, supported_team.id, match)
        return data


class PlayerEvaluationSerializer(serializers.ModelSerializer):
    """Оценка игрока."""

    player_name = serializers.CharField(source="player.first_name", read_only=True)
    player_last_name = serializers.CharField(source="player.last_name", read_only=True)
    player_number = serializers.IntegerField(
        source="player.number", read_only=True, allow_null=True
    )
    match_details = MatchSerializer(source="match", read_only=True)
    maturity_score = serializers.IntegerField(read_only=True)

    class Meta:
        model = PlayerEvaluation
        fields = (
            "id",
            "match",
            "match_details",
            "player",
            "player_name",
            "player_last_name",
            "player_number",
            "contribution",
            "risk",
            "potential",
            "maturity_score",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "maturity_score")

    def validate_match(self, value: Match) -> Match:
        _run_policy(assert_voting_open, value)
        return value

    def validate(self, data: dict) -> dict:
        _forbid_identity_field_changes(self.instance, data, ("match", "player"))
        request = self.context.get("request")
        _forbid_after_completion(
            request.user if request else None,
            data.get("match") or (self.instance.match if self.instance else None),
        )

        request = self.context.get("request")
        user = request.user if request else None
        match = data.get("match")
        player = data.get("player")
        if user and match:
            existing = PlayerEvaluation.objects.filter(
                user=user, match=match, player=player
            ).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили этого игрока в данном матче")

            if not self.instance:
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)

            if player is not None:
                _run_policy(assert_player_in_squad, player.id, match)

            for field_name in ("contribution", "risk", "potential"):
                field_value = data.get(field_name)
                if field_value is not None and (field_value < 1 or field_value > 10):
                    raise serializers.ValidationError(f"{field_name} должен быть от 1 до 10")
        return data


class TeamEvaluationSerializer(serializers.ModelSerializer):
    """Оценка команды."""

    team_name = serializers.CharField(source="team.name", read_only=True)
    match_details = MatchSerializer(source="match", read_only=True)
    average_score = serializers.FloatField(read_only=True)

    class Meta:
        model = TeamEvaluation
        fields = (
            "id",
            "match",
            "match_details",
            "team",
            "team_name",
            "tactics",
            "effort",
            "organization",
            "mentality",
            "average_score",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "average_score")

    def validate_match(self, value: Match) -> Match:
        _run_policy(assert_voting_open, value)
        return value

    def validate(self, data: dict) -> dict:
        _forbid_identity_field_changes(self.instance, data, ("match", "team"))
        request = self.context.get("request")
        _forbid_after_completion(
            request.user if request else None,
            data.get("match") or (self.instance.match if self.instance else None),
        )

        request = self.context.get("request")
        user = request.user if request else None
        match = data.get("match")
        team = data.get("team")
        if user and match:
            existing = TeamEvaluation.objects.filter(
                user=user, match=match, team=team
            ).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили эту команду в данном матче")

            if not self.instance:
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)

            if team is not None:
                _run_policy(assert_team_in_match, team.id, match)
        return data


class CoachEvaluationSerializer(serializers.ModelSerializer):
    """Оценка тренера."""

    coach_name = serializers.CharField(source="coach.first_name", read_only=True)
    coach_last_name = serializers.CharField(source="coach.last_name", read_only=True)
    match_details = MatchSerializer(source="match", read_only=True)
    average_score = serializers.FloatField(read_only=True)

    class Meta:
        model = CoachEvaluation
        fields = (
            "id",
            "match",
            "match_details",
            "coach",
            "coach_name",
            "coach_last_name",
            "tactics",
            "substitutions",
            "game_management",
            "impact",
            "average_score",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "average_score")

    def validate_match(self, value: Match) -> Match:
        _run_policy(assert_voting_open, value)
        return value

    def validate(self, data: dict) -> dict:
        _forbid_identity_field_changes(self.instance, data, ("match", "coach"))
        request = self.context.get("request")
        _forbid_after_completion(
            request.user if request else None,
            data.get("match") or (self.instance.match if self.instance else None),
        )

        request = self.context.get("request")
        user = request.user if request else None
        match = data.get("match")
        coach = data.get("coach")
        if user and match:
            existing = CoachEvaluation.objects.filter(
                user=user, match=match, coach=coach
            ).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили этого тренера в данном матче")

            if not self.instance:
                # Нужен контекст просмотра.
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)

            if coach is not None:
                _run_policy(assert_coach_in_match, coach.id, match)
        return data


class RefereeEvaluationSerializer(serializers.ModelSerializer):
    """Оценка судейства."""

    match_details = MatchSerializer(source="match", read_only=True)

    class Meta:
        model = RefereeEvaluation
        fields = (
            "id",
            "match",
            "match_details",
            "influence_score",
            "decision_quality",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_match(self, value: Match) -> Match:
        _run_policy(assert_voting_open, value)
        return value

    def validate(self, data: dict) -> dict:
        _forbid_identity_field_changes(self.instance, data, ("match",))
        request = self.context.get("request")
        _forbid_after_completion(
            request.user if request else None,
            data.get("match") or (self.instance.match if self.instance else None),
        )

        request = self.context.get("request")
        user = request.user if request else None
        match = data.get("match")
        if user and match:
            existing = RefereeEvaluation.objects.filter(user=user, match=match).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили судейство этого матча")

            if not self.instance:
                # Нужен контекст просмотра.
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)

            influence_score = data.get("influence_score")
            if influence_score is not None and (influence_score < 0 or influence_score > 100):
                raise serializers.ValidationError("influence_score должен быть от 0 до 100")
        return data


class MatchEvaluationSerializer(serializers.ModelSerializer):
    """Общая оценка матча."""

    match_details = MatchSerializer(source="match", read_only=True)
    drama_index = serializers.IntegerField(read_only=True)

    class Meta:
        model = MatchEvaluation
        fields = (
            "id",
            "match",
            "match_details",
            "entertainment",
            "tension",
            "turning_point",
            "fairness",
            "drama_index",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at", "drama_index")

    def validate_match(self, value: Match) -> Match:
        _run_policy(assert_voting_open, value)
        return value

    def validate(self, data: dict) -> dict:
        _forbid_identity_field_changes(self.instance, data, ("match",))
        request = self.context.get("request")
        _forbid_after_completion(
            request.user if request else None,
            data.get("match") or (self.instance.match if self.instance else None),
        )

        request = self.context.get("request")
        user = request.user if request else None
        match = data.get("match")
        if user and match:
            existing = MatchEvaluation.objects.filter(user=user, match=match).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили этот матч")

            if not self.instance:
                # Нужен контекст просмотра.
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)
        return data


# ============================================================================
# === АГРЕГАТЫ ===
# ============================================================================


class PlayerMatchAggregateSerializer(serializers.ModelSerializer):
    """Агрегаты игрока."""

    player_name = serializers.CharField(source="player.first_name", read_only=True)
    player_last_name = serializers.CharField(source="player.last_name", read_only=True)
    match_details = MatchSerializer(source="match", read_only=True)

    performance_score = serializers.FloatField(
        read_only=True,
        label="Рейтинг выступления",
        help_text="Общая оценка выступления игрока в матче",
    )
    risk_index = serializers.FloatField(
        read_only=True,
        label="Индекс риска",
        help_text="Индекс рискованных действий игрока",
    )
    maturity_score = serializers.FloatField(
        read_only=True,
        label="Индекс зрелости",
        help_text="Индекс зрелости игрока",
    )
    stability_index = serializers.FloatField(
        read_only=True,
        label="Индекс стабильности",
        help_text="Индекс стабильности выступлений",
    )
    clutch_index = serializers.FloatField(
        read_only=True,
        label="Индекс решающих моментов",
        help_text="Эффективность в ключевых моментах",
    )

    class Meta:
        model = PlayerMatchAggregate
        fields = (
            "id",
            "player",
            "player_name",
            "player_last_name",
            "match",
            "match_details",
            "avg_contribution",
            "avg_risk",
            "avg_potential",
            "total_votes",
            "performance_score",
            "risk_index",
            "maturity_score",
            "stability_index",
            "clutch_index",
        )
        read_only_fields = (
            "id",
            "player",
            "match",
            "player_name",
            "player_last_name",
            "match_details",
            "avg_contribution",
            "avg_risk",
            "avg_potential",
            "total_votes",
            "performance_score",
            "risk_index",
            "maturity_score",
            "stability_index",
            "clutch_index",
        )


class MatchAggregateSerializer(serializers.ModelSerializer):
    """Агрегаты матча."""

    match_details = MatchSerializer(source="match", read_only=True)

    class Meta:
        model = MatchAggregate
        fields = (
            "id",
            "match",
            "match_details",
            "avg_entertainment",
            "avg_tension",
            "avg_fairness",
            "turning_point_ratio",
            "total_votes",
            "drama_index",
        )
        read_only_fields = (
            "id",
            "match",
            "match_details",
            "avg_entertainment",
            "avg_tension",
            "avg_fairness",
            "turning_point_ratio",
            "total_votes",
            "drama_index",
        )


class CoachMatchAggregateSerializer(serializers.ModelSerializer):
    """Агрегаты тренера."""

    coach_name = serializers.CharField(source="coach.first_name", read_only=True)
    coach_last_name = serializers.CharField(source="coach.last_name", read_only=True)
    match_details = MatchSerializer(source="match", read_only=True)

    class Meta:
        model = CoachMatchAggregate
        fields = (
            "id",
            "coach",
            "coach_name",
            "coach_last_name",
            "match",
            "match_details",
            "avg_tactics",
            "avg_substitutions",
            "avg_management",
            "avg_impact",
            "total_votes",
        )
        read_only_fields = (
            "id",
            "coach",
            "match",
            "coach_name",
            "coach_last_name",
            "match_details",
            "avg_tactics",
            "avg_substitutions",
            "avg_management",
            "avg_impact",
            "total_votes",
        )