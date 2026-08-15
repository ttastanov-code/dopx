# events/views.py
"""
Live-пульс (продуктовый аудит DOPX, раздел 2 "Live-слой"). HTMX-эндпоинты:
тап по реакции меняет ОДНУ строку в БД и возвращает крошечный HTML-фрагмент
(пара кнопок), а не полную страницу — иначе на популярном матче с сотнями
зрителей, тапающих одновременно, каждый тап гонял бы килобайты разметки
туда-обратно без необходимости. Опрос (`hx-trigger="every 15s"`, см.
`templates/events/_live_pulse.html`) обновляет счётчики у ВСЕХ зрителей
без WebSocket-инфраструктуры — Django Channels был бы избыточен для
интервала 15-20с, который сам продуктовый документ считает приемлемым.
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
        # ИСПРАВЛЕНО: было status=401. HTMX по умолчанию swap'ает контент
        # ТОЛЬКО на 2xx-ответах (см. htmx.config.responseHandling) — с 401
        # этот фрагмент рендерился на сервере, но клиент его молча
        # отбрасывал: тап анонима визуально не давал НИКАКОЙ обратной связи
        # (кнопка просто не менялась), хотя намерение кода (см. комментарий
        # ниже) — как раз показать призыв войти. 200 здесь корректен: это
        # не ошибка сервера, а осознанный alternate-фрагмент интерфейса.
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
