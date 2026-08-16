# events/services.py
"""Сервисный слой live-пульса: тап/подсчёт реакций, отдельно от views.py."""
from __future__ import annotations

from django.db.models import Count, Q

from .models import EventReaction, MatchEvent


def toggle_reaction(*, user, match_event: MatchEvent, reaction: str) -> str | None:
    """
    Тап по 👍/👎. Идемпотентно относительно повторного тапа по ТОЙ ЖЕ
    реакции — второй тап по 👍 убирает реакцию (toggle-off), а не создаёт
    дубликат/ошибку UniqueConstraint. Тап по противоположной реакции
    ЗАМЕНЯЕТ существующую (нельзя одновременно 👍 и 👎 одно и то же
    событие — это не два независимых счётчика лайков и дизлайков, а один
    выбор стороны).

    Возвращает итоговую реакцию пользователя после тапа: 'like' / 'dislike'
    / None (если реакция была снята).
    """
    existing = EventReaction.objects.filter(match_event=match_event, user=user).first()

    if existing is None:
        EventReaction.objects.create(match_event=match_event, user=user, reaction=reaction)
        return reaction

    if existing.reaction == reaction:
        existing.delete()
        return None

    existing.reaction = reaction
    existing.save(update_fields=['reaction', 'updated_at'])
    return reaction


def reaction_counts(match_event_ids: list) -> dict:
    """
    Один запрос на список событий вместо N — та же логика экономии
    запросов, что в `aggregates/services.py::_build_user_weight_map`.
    Возвращает {event_id: {'like': N, 'dislike': N}}.
    """
    rows = (
        EventReaction.objects.filter(match_event_id__in=match_event_ids)
        .values('match_event_id')
        .annotate(
            like_count=Count('id', filter=Q(reaction='like')),
            dislike_count=Count('id', filter=Q(reaction='dislike')),
        )
    )
    return {
        row['match_event_id']: {'like': row['like_count'], 'dislike': row['dislike_count']}
        for row in rows
    }


def user_reactions_map(user, match_event_ids: list) -> dict:
    """{event_id: 'like'|'dislike'} только для реакций ЭТОГО пользователя —
    нужно, чтобы подсветить кнопку, которую он уже нажал."""
    if not user or not user.is_authenticated:
        return {}
    return dict(
        EventReaction.objects.filter(
            match_event_id__in=match_event_ids, user=user
        ).values_list('match_event_id', 'reaction')
    )
