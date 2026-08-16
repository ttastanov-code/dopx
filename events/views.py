# events/views.py
"""
Live-пульс. Тап по реакции меняет одну строку в БД и возвращает крошечный
HTML-фрагмент (пара кнопок), не всю страницу — иначе на популярном матче
с сотнями одновременных тапов каждый гонял бы лишние килобайты разметки.
Опрос every 15s (templates/events/_live_pulse.html) вместо WebSocket/Channels
— на таком интервале это избыточная инфраструктура.
"""
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from matches.models import Match

from .models import EventReaction, MatchEvent
from .services import reaction_counts, toggle_reaction, user_reactions_map

# Показываем реакции только у "крупных" событий — гол, пенальти, карточки,
# VAR. Замены/автоголы формально MatchEvent, но эмоционально нейтральны,
# реагировать на них 👍/👎 бессмысленно и засоряет ленту пульса.
PULSE_EVENT_TYPES = ["goal", "penalty", "own_goal", "yellow_card", "red_card", "var_check"]
PULSE_EVENTS_LIMIT = 12


@require_GET
def pulse_partial(request, match_id):
    """HTMX-партиал: последние live-события матча с кнопками реакции."""
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
    """Тап по 👍/👎. Возвращает обновлённую пару кнопок для ОДНОГО события."""
    if not request.user.is_authenticated:
        # status=200, не 401 — HTMX по умолчанию swap'ает контент только на
        # 2xx (htmx.config.responseHandling), иначе призыв войти рендерится,
        # но клиент его молча отбрасывает.
        return render(
            request, 'events/_reaction_login_prompt.html', {'event_id': event_id}, status=200
        )

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
