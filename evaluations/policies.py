# evaluations/policies.py
"""
EvaluationPolicy — единая точка правды о том, что именно разрешено
оценивать в рамках матча.

ПОЧЕМУ ЭТОТ МОДУЛЬ ПОЯВИЛСЯ (2026-09-04, см. ADR-0001 и
docs/CODEX_AUDIT_RESPONSE_2026-09-04.md): до этого правила голосования были
разбросаны по трём независимым местам, которые физически не могли
разойтись до этого момента только по совпадению:

  1. evaluations/forms.py — веб-форма ограничивала выбор через querysets
     (ContextEvaluationForm.supported_team) и через сам способ генерации
     полей (TeamEvaluationForm/PlayerEvaluationForm/CoachEvaluationForm
     создают поля ТОЛЬКО для реальных участников матча — подделать нечего,
     в форме просто нет такого поля).
  2. api/serializers.py — принимал произвольный `player`/`team`/`coach` из
     тела запроса и проверял только уникальность голоса (user+match+entity)
     и открытое окно голосования. Принадлежность сущности МАТЧУ нигде не
     проверялась.

Итог: верифицированный пользователь мог через прямой POST в API оценить
игрока, не игравшего в этом матче, команду, которая в нём не участвовала,
тренера, не назначенного на матч — и такая оценка ничем не отличалась бы
от настоящей при подсчёте агрегатов, тура и сезонной сборной.

Теперь и forms.py (в ContextEvaluationForm — единственном месте веб-формы,
где технически возможно подменить ID), и serializers.py вызывают ОДНИ И ТЕ
ЖЕ функции. Дублирования правил больше нет: если завтра появится новое
правило голосования, его нужно поменять здесь, а не искать все места,
где оно было продублировано.

Стиль: каждая функция — "assert"-проверка, либо молча проходит, либо
поднимает EvaluationPolicyError с готовым пользовательским текстом на
русском (тем же текстом, что раньше был захардкожен в каждом отдельном
validate()). Вызывающая сторона сама решает, во что завернуть исключение —
DRF-сериалайзер оборачивает в serializers.ValidationError, Django-форма — в
forms.ValidationError.
"""
from __future__ import annotations

from django.utils import timezone

from lineups.models import MatchLineupPlayer
from matches.models import Match


class EvaluationPolicyError(Exception):
    """Единое исключение для любого нарушения правил голосования."""


def assert_voting_open(match: Match) -> None:
    """Матч должен быть начат и окно голосования — ещё открыто.

    Раньше эта же пара проверок была продублирована по одной в КАЖДОМ из
    шести `validate_match()` в api/serializers.py — дословно одинаковый
    код, скопированный шесть раз.
    """
    now = timezone.now()
    if now < match.start_time:
        raise EvaluationPolicyError('Голосование откроется после начала матча')
    if now > match.voting_open_until:
        raise EvaluationPolicyError('Голосование для этого матча закрыто')


def assert_context_exists(context_evaluation_exists: bool) -> None:
    """Контекст просмотра (шаг 1 вайзарда) должен быть создан раньше любой
    предметной оценки (игрока/команды) — иначе на агрегаты может повлиять
    голос человека, который даже не подтвердил, что смотрел матч.

    Принимает уже вычисленный булев результат, а не (user, match) — вызывающая
    сторона обычно и так делает `.exists()` в рамках более широкого запроса
    (см. PlayerEvaluationSerializer.validate), пересчитывать его здесь ещё
    раз было бы лишним запросом к БД.
    """
    if not context_evaluation_exists:
        raise EvaluationPolicyError('Сначала укажите контекст просмотра матча')


def assert_team_in_match(team_id: int, match: Match) -> None:
    """Команда должна быть домашней или гостевой в ЭТОМ матче — иначе можно
    было бы, например, оценить тактику "Кайрата" в матче, где он не играл."""
    if team_id not in (match.home_team_id, match.away_team_id):
        raise EvaluationPolicyError('Эта команда не участвовала в данном матче')


def assert_player_in_squad(player_id: int, match: Match) -> None:
    """Игрок должен быть в заявке (lineup) именно этого матча — единственная
    проверка, которую нельзя выразить простым сравнением ID (как для
    команд/тренеров), нужен запрос к MatchLineupPlayer."""
    in_squad = MatchLineupPlayer.objects.filter(
        lineup__match=match, player_id=player_id
    ).exists()
    if not in_squad:
        raise EvaluationPolicyError('Этот игрок не входил в заявку на данный матч')


def assert_coach_in_match(coach_id: int, match: Match) -> None:
    """Тренер должен быть назначен на одну из двух команд ИМЕННО в этом
    матче (match.home_coach/away_coach — снимок на момент матча, а не
    просто "текущий тренер команды", у команды он мог смениться)."""
    if coach_id not in (match.home_coach_id, match.away_coach_id):
        raise EvaluationPolicyError('Этот тренер не участвовал в данном матче')
