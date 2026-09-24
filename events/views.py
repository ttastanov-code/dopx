# events/views.py
"""Live-пульс: тап меняет одну строку и возвращает пару кнопок. Опрос каждые 15 с."""
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from core.utils import is_rate_limited
from matches.models import Match

from .models import EventReaction, MatchEvent
from .services import reaction_counts, toggle_reaction, user_reactions_map

# Реакции только у крупных событий (голы, пенальти, карточки, VAR).
PULSE_EVENT_TYPES = ["goal", "penalty", "own_goal", "yellow_card", "red_card", "var_check"]
PULSE_EVENTS_LIMIT = 12

# Rate-limit по user.id.
REACT_RATE_LIMIT = 30
REACT_RATE_LIMIT_WINDOW_SECONDS = 60


@require_GET
def pulse_partial(request, match_id):
    """HTMX-партиал: последние события с кнопками реакций."""
    match = get_object_or_404(Match, id=match_id)
    events = list(
        match.events.filter(event_type__in=PULSE_EVENT_TYPES)
        .select_related('player')
        .order_by('-minute', '-added_time')[:PULSE_EVENTS_LIMIT]
    )
    event_ids = [e.id for e in events]
    counts = reaction_counts(event_ids)
    user_reactions = user_reactions_map(request.user, event_ids)

    return render(request, 'events/_live_pulse.html', {
        'match': match,
        'events': events,
        'counts': counts,
        'user_reactions': user_reactions,
    })


@require_POST
def react_to_event(request, event_id):
    """Тап по 👍/👎 — обновлённая пара кнопок. Лимит 30/мин; при превышении 429."""
    if not request.user.is_authenticated:
        # 200, не 401 — HTMX свапает только 2xx.
        return render(
            request, 'events/_reaction_login_prompt.html', {'event_id': event_id}, status=200
        )

    if is_rate_limited(f'react_to_event:{request.user.id}', REACT_RATE_LIMIT, REACT_RATE_LIMIT_WINDOW_SECONDS):
        return HttpResponse(status=429)

    reaction = request.POST.get('reaction')
    if reaction not in dict(EventReaction.REACTION_CHOICES):
        return HttpResponseNotAllowed(['POST'])

    event = get_object_or_404(MatchEvent, id=event_id)
    toggle_reaction(user=request.user, match_event=event, reaction=reaction)

    counts = reaction_counts([event.id])
    user_reactions = user_reactions_map(request.user, [event.id])

    return render(request, 'events/_reaction_buttons.html', {
        'event': event,
        'counts': counts,
        'user_reactions': user_reactions,
    })
