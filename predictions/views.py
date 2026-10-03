# predictions/views.py
"""Прогноз 1X2: клик обновляет строку в БД и возвращает виджет (hx-swap=outerHTML)."""
from functools import partial

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET, require_POST

from analytics.models import EventName
from analytics.services import track_event
from core.utils import is_rate_limited
from matches.models import Match
from users.tasks import check_and_award_badges_task

from .models import MatchPrediction
from .services import prediction_counts, submit_prediction, user_prediction

# Rate-limit по user.id.
PREDICT_RATE_LIMIT = 20
PREDICT_RATE_LIMIT_WINDOW_SECONDS = 60


def _widget_context(request, match):
    my_prediction = user_prediction(request.user, match)
    challenge_url = ''
    if my_prediction and match.is_prediction_open:
        from django.urls import reverse

        from engagement.referrals import code_for
        challenge_url = request.build_absolute_uri(
            reverse('engagement:challenge', args=[code_for(request.user), match.id])
        )
    return {
        'match': match,
        'counts': prediction_counts(match),
        'my_prediction': my_prediction,
        'challenge_url': challenge_url,
    }


@require_GET
def prediction_widget_partial(request, match_id):
    """Ленивая загрузка виджета (hx-trigger=load)."""
    match = get_object_or_404(Match, id=match_id)
    return render(request, 'predictions/_prediction_widget.html', _widget_context(request, match))


@require_POST
def predict(request, match_id):
    """Клик по П1/Х/П2 — возвращает виджет целиком."""
    match = get_object_or_404(Match, id=match_id)

    # compact=1 — ответ компактным виджетом для карточки в списке.
    compact = request.POST.get('compact') == '1'
    widget_template = 'predictions/_prediction_widget_compact.html' if compact else 'predictions/_prediction_widget.html'
    login_template = (
        'predictions/_prediction_login_prompt_compact.html' if compact
        else 'predictions/_prediction_login_prompt.html'
    )

    if not request.user.is_authenticated:
        # 200, не 401 — HTMX свапает только 2xx.
        return render(request, login_template, {'match': match}, status=200)

    from django.conf import settings

    from users.emails import profile_complete
    if settings.PROFILE_REQUIRED and not profile_complete(request.user):
        return render(request, 'predictions/_prediction_profile_prompt.html', {'match': match, 'compact': compact})

    if is_rate_limited(
        f'predict:{request.user.id}', PREDICT_RATE_LIMIT, PREDICT_RATE_LIMIT_WINDOW_SECONDS
    ):
        return HttpResponse(status=429)

    choice = request.POST.get('choice')
    if choice not in dict(MatchPrediction.CHOICE_CHOICES):
        return HttpResponse(status=400)

    prediction, created = submit_prediction(user=request.user, match=match, choice=choice)
    if prediction is not None:
        track_event(
            EventName.PREDICTION_MADE, request=request,
            properties={'match_id': str(match.id), 'choice': choice},
        )
        if created:
            # Серия обновляется после матча; бейдж first_prediction — через on_commit.
            transaction.on_commit(
                partial(check_and_award_badges_task.delay, user_id=str(request.user.id), match_id=str(match.id))
            )
    # None — окно закрылось между загрузкой и кликом.

    return render(request, widget_template, _widget_context(request, match))
