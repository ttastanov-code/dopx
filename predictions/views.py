# predictions/views.py
"""
Краудсорс-прогноз 1X2. Тот же паттерн, что и `events/views.py`: клик по
опции обновляет одну строку в БД и возвращает крошечный HTML-партиал
(виджет целиком, не всю страницу матча), HTMX сам меняет DOM
(`hx-swap="outerHTML"`).
"""
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

# По user.id — эндпоинт требует аутентификации (см. predict() ниже), так
# что user.id уже доступен и точнее IP (NAT/мобильные сети), тот же выбор,
# что и у events/views.py::REACT_RATE_LIMIT. Лимит щедрее, чем у реакций,
# потому что переголосовать можно максимум 3 раза осмысленно (П1→Х→П2), а
# не десятками кликов по ленте событий.
PREDICT_RATE_LIMIT = 20
PREDICT_RATE_LIMIT_WINDOW_SECONDS = 60


def _widget_context(request, match):
    return {
        'match': match,
        'counts': prediction_counts(match),
        'my_prediction': user_prediction(request.user, match),
    }


@require_GET
def prediction_widget_partial(request, match_id):
    """HTMX-партиал для ленивой загрузки виджета на странице матча
    (`hx-trigger="load"`, тот же паттерн, что `events:pulse`)."""
    match = get_object_or_404(Match, id=match_id)
    return render(request, 'predictions/_prediction_widget.html', _widget_context(request, match))


@require_POST
def predict(request, match_id):
    """
    Клик по П1/Х/П2. Возвращает обновлённый виджет целиком (проценты
    меняются у ВСЕХ трёх опций разом при каждом новом голосе, в отличие от
    events:react, где можно обновить один счётчик).
    """
    match = get_object_or_404(Match, id=match_id)

    if not request.user.is_authenticated:
        # status=200, не 401 — HTMX по умолчанию свапает контент только на
        # 2xx, см. идентичный комментарий в events/views.py::react_to_event.
        return render(request, 'predictions/_prediction_login_prompt.html', {'match': match}, status=200)

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
            # Серия/бейджи считаются только на ПЕРВУЮ ставку на этот матч
            # (см. docstring submit_prediction) — смена П1→Х до старта не
            # должна давать повторный тик серии. update_prediction_stats()
            # — синхронная запись (дешёвая, одна строка), а проверка
            # бейджей — асинхронно через transaction.on_commit, тот же
            # принцип, что и evaluations/views.py::EvaluateMatchFinalView
            # (до ~15 запросов внутри check_and_award_badges, незачем
            # держать ими HTTP-цикл).
            request.user.update_prediction_stats()
            transaction.on_commit(
                partial(check_and_award_badges_task.delay, user_id=str(request.user.id), match_id=str(match.id))
            )
    # prediction is None, если окно голосования закрылось между открытием
    # страницы и кликом (гонка на старте матча) — виджет просто
    # перерисуется в закрытом состоянии, без ошибки пользователю.

    return render(request, 'predictions/_prediction_widget.html', _widget_context(request, match))
