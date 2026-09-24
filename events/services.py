# events/services.py
"""Live-пульс: реакции на события."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from .models import EventReaction, MatchEvent


def toggle_reaction(*, user, match_event: MatchEvent, reaction: str) -> str | None:
    """Тап по 👍/👎: повтор той же — снимает, противоположная — заменяет.
    Возвращает 'like' / 'dislike' / None.
    """
    # Двойной тап — обрабатываем IntegrityError.
    with transaction.atomic():
        existing = EventReaction.objects.select_for_update().filter(
            match_event=match_event, user=user
        ).first()

        if existing is None:
            try:
                # Savepoint — ошибка вставки не ломает внешнюю транзакцию.
                with transaction.atomic():
                    EventReaction.objects.create(match_event=match_event, user=user, reaction=reaction)
                return reaction
            except IntegrityError:
                existing = EventReaction.objects.select_for_update().get(
                    match_event=match_event, user=user
                )

        if existing.reaction == reaction:
            existing.delete()
            return None

        existing.reaction = reaction
        existing.save(update_fields=['reaction', 'updated_at'])
        return reaction


def reaction_counts(match_event_ids: list) -> dict:
    """Счётчики реакций для списка событий одним запросом: {event_id: {'like', 'dislike'}}."""
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
    """{event_id: 'like'|'dislike'} текущего пользователя."""
    if not user or not user.is_authenticated:
        return {}
    return dict(
        EventReaction.objects.filter(
            match_event_id__in=match_event_ids, user=user
        ).values_list('match_event_id', 'reaction')
    )
