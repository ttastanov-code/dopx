# evaluations/policies.py
"""EvaluationPolicy — правила, что можно оценивать в рамках матча.
Используется и формами (evaluations/forms.py), и API (api/serializers.py).
Каждая функция либо проходит молча, либо бросает EvaluationPolicyError с текстом для пользователя.
"""
from __future__ import annotations

from django.utils import timezone

from lineups.models import MatchLineupPlayer
from matches.models import Match


class EvaluationPolicyError(Exception):
    """Нарушение правил голосования."""


def assert_voting_open(match: Match) -> None:
    """Матч завершён и голосование ещё открыто — одно правило для сайта и API."""
    if timezone.now() > match.voting_open_until:
        raise EvaluationPolicyError('Голосование для этого матча закрыто')
    if match.status != 'finished':
        raise EvaluationPolicyError('Голосование доступно только для завершённых матчей')


def assert_context_exists(context_evaluation_exists: bool) -> None:
    """Контекст просмотра должен быть создан раньше предметной оценки.
    Принимает уже посчитанный bool.
    """
    if not context_evaluation_exists:
        raise EvaluationPolicyError('Сначала укажите контекст просмотра матча')


def assert_team_in_match(team_id: int, match: Match) -> None:
    """Команда — хозяева или гости этого матча."""
    if team_id not in (match.home_team_id, match.away_team_id):
        raise EvaluationPolicyError('Эта команда не участвовала в данном матче')


def assert_player_in_squad(player_id: int, match: Match) -> None:
    """Игрок — в заявке этого матча."""
    in_squad = MatchLineupPlayer.objects.filter(
        lineup__match=match, player_id=player_id
    ).exists()
    if not in_squad:
        raise EvaluationPolicyError('Этот игрок не входил в заявку на данный матч')


def assert_coach_in_match(coach_id: int, match: Match) -> None:
    """Тренер — назначен на этот матч (home_coach/away_coach)."""
    if coach_id not in (match.home_coach_id, match.away_coach_id):
        raise EvaluationPolicyError('Этот тренер не участвовал в данном матче')
