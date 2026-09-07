# api/serializers.py
"""
DRF-сериалайзеры.

DRF-COMPLIANCE FIX: во ВСЕХ сериалайзерах `read_only_fields` были заданы как
Python `list` (`[...]`), а не `tuple` (`(...,)`), как того требует соглашение
Meta-опций DRF (в официальной документации и исходниках DRF `read_only_fields`
трактуется как неизменяемая последовательность). Практическое следствие:
1) `drf-spectacular` при интроспекции Meta-класса в отдельных версиях
   полагается на неизменяемость этого атрибута при построении схемы и кэша
   полей сериалайзера — list, будучи мутабельным, может быть случайно
   изменён где-то в рантайме (например, `+= ['field']` в наследнике или в
   миксине), что молча испортит схему OpenAPI для ВСЕХ последующих
   запросов, использующих тот же класс (Meta-атрибуты живут на уровне
   класса, а не экземпляра).
2) list как дефолтный мутабельный аргумент класса — источник классической
   Python-ошибки "shared mutable state" при наследовании сериалайзеров.
Исправление: везде заменено на `tuple`.
"""
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
    """Прогоняет одну проверку из evaluations/policies.py и переводит её
    исключение в serializers.ValidationError — единственное место, где
    политика "знает" про DRF (сам модуль policies.py от DRF не зависит,
    его же вызывает и evaluations/forms.py, где нужен forms.ValidationError).
    См. docs/adr/0001-evaluation-policy-single-source-of-truth.md."""
    try:
        check(*args)
    except EvaluationPolicyError as e:
        raise serializers.ValidationError(str(e)) from e


def _forbid_identity_field_changes(instance, data: dict, field_names: tuple[str, ...]) -> None:
    """Запрещает менять поля, определяющие ЛИЧНОСТЬ оценки (какой матч/кого
    оценивают), при PATCH/PUT уже существующей записи — см.
    docs/adr/0027-lock-evaluation-identity-fields-on-update.md.

    Найдено внешним аудитом (docs/CODEX_AUDIT_RESPONSE_2026-09-07.md):
    validate() ниже брал match/player/team/coach из `data.get(...)`, и при
    partial-обновлении (PATCH) с полем, ОТСУТСТВУЮЩИМ в теле запроса,
    `data.get(...)` возвращал None — вся проверка `if user and match: ...`
    (включая EvaluationPolicy: assert_player_in_squad и т.д.) тихо
    пропускалась целиком. Хуже того: даже если поле ПРИСУТСТВОВАЛО в PATCH
    (например, "player": 999 без "match"), уникальность и принадлежность
    матчу всё равно не проверялись, потому что match отсутствовал в data.
    Итог: владелец собственной оценки мог создать её честно, а затем прямым
    PATCH подменить player/team/coach/match на что угодно — агрегаты и
    сезонная сборная не отличили бы такую оценку от настоящей.

    Фикс — не пытаться закрыть все комбинации в самой validate() (легко
    забыть новую), а полностью запретить менять эти поля после создания:
    после POST разрешено менять только сами баллы. Сравнение через `_id`,
    а не сам объект — чтобы не делать лишний SELECT, если поле не менялось."""
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
    """Сериалайзер матча для вложенных данных."""

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
    """Сериалайзер контекста просмотра матча."""

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
        """Проверка уникальности: user + match, и что выбранная
        поддерживаемая команда реально участвовала в матче (см.
        docs/adr/0001-evaluation-policy-single-source-of-truth.md)."""
        _forbid_identity_field_changes(self.instance, data, ("match", "supported_team"))

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
    """Сериалайзер оценки игрока."""

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
    """Сериалайзер оценки команды."""

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
    """Сериалайзер оценки тренера."""

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
                # Единообразие с Player/Team-оценками (см.
                # docs/CODEX_AUDIT_RESPONSE_2026-09-07.md): контекст
                # просмотра — элемент доверия к голосу, требуется для
                # ЛЮБОГО типа предметной оценки, не только для игрока/команды.
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)

            if coach is not None:
                _run_policy(assert_coach_in_match, coach.id, match)
        return data


class RefereeEvaluationSerializer(serializers.ModelSerializer):
    """Сериалайзер оценки судейства."""

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
        user = request.user if request else None
        match = data.get("match")
        if user and match:
            existing = RefereeEvaluation.objects.filter(user=user, match=match).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили судейство этого матча")

            if not self.instance:
                # Единообразие с остальными типами оценок — см.
                # docs/CODEX_AUDIT_RESPONSE_2026-09-07.md.
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)

            influence_score = data.get("influence_score")
            if influence_score is not None and (influence_score < 0 or influence_score > 100):
                raise serializers.ValidationError("influence_score должен быть от 0 до 100")
        return data


class MatchEvaluationSerializer(serializers.ModelSerializer):
    """Сериалайзер общей оценки матча."""

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
        user = request.user if request else None
        match = data.get("match")
        if user and match:
            existing = MatchEvaluation.objects.filter(user=user, match=match).exists()
            if existing and not self.instance:
                raise serializers.ValidationError("Вы уже оценили этот матч")

            if not self.instance:
                # Единообразие с остальными типами оценок — см.
                # docs/CODEX_AUDIT_RESPONSE_2026-09-07.md.
                context_exists = ContextEvaluation.objects.filter(user=user, match=match).exists()
                _run_policy(assert_context_exists, context_exists)
        return data


# ============================================================================
# === АГРЕГАТЫ ===
# ============================================================================


class PlayerMatchAggregateSerializer(serializers.ModelSerializer):
    """Сериалайзер агрегатов игрока."""

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
    """Сериалайзер агрегатов матча."""

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
        # FIX: read_only_fields — tuple, а не list (см. docstring модуля)
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
    """Сериалайзер агрегатов тренера."""

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
        # FIX: read_only_fields — tuple, а не list (см. docstring модуля)
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