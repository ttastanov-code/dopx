# predictions/services.py
"""Сервисный слой прогнозов 1X2 — та же граница ответственности, что и
events/services.py: views.py не трогает модель напрямую."""
from __future__ import annotations

from django.db.models import Count, Q

from .models import MatchPrediction


def submit_prediction(*, user, match, choice: str) -> tuple[MatchPrediction, bool] | tuple[None, bool]:
    """
    Ставит или меняет прогноз пользователя на матч. В отличие от
    `events/services.py::toggle_reaction` — здесь НЕТ toggle-off повторным
    выбором той же опции: формальный прогноз, снятый без замены другим,
    не имеет смысла (это не лайк "на эмоции"). Повторный POST с уже
    выбранной опцией — no-op через `update_or_create` (перезаписывает
    той же самой строкой).

    Окно голосования проверяется ЗДЕСЬ ЕЩЁ РАЗ, не только в
    views.py/шаблоне: HTMX POST можно отправить напрямую (curl/devtools),
    минуя задизейбленную в UI кнопку — см. `Match.is_prediction_open()`.
    Возвращает `(None, False)`, если окно уже закрыто (гонка: пользователь
    открыл страницу до старта, кликнул уже после) — вызывающий код
    (views.py) решает, как это показать.

    Возвращает флаг `created` ОТДЕЛЬНО от самого прогноза — views.py должен
    проверить бейдж "первая ставка" (`check_and_award_badges_task`) только
    при ПЕРВОЙ ставке на этот матч, не при каждой смене выбора. Серия
    прогнозов (`User.update_prediction_stats()`) — БОЛЬШЕ НЕ здесь и не в
    views.py: она означает "подряд угаданных исходов" и обновляется только
    когда матч завершается, см. notifications/tasks.py::
    notify_prediction_results. Сама функция НЕ трогает `User`/Celery — это
    HTTP-независимый сервисный слой, побочные эффекты уровня "пользователь
    + асинхронные задачи" остаются в views.py, как и у evaluations/events.
    """
    if not match.is_prediction_open():
        return None, False
    prediction, created = MatchPrediction.objects.update_or_create(
        match=match, user=user, defaults={'choice': choice},
    )
    return prediction, created


def prediction_counts(match) -> dict:
    """
    Один запрос на матч — доли голосов по каждой из трёх опций. Проценты
    округляются до целого (`round()`, не `floatformat`) прямо в Python, а
    не в шаблоне — три отдельных float-деления в шаблоне менее читаемы и
    не гарантируют согласованность округления между барами.

    Сознательно НЕ материализуется в отдельную agregate-модель (в отличие
    от `aggregates.MatchAggregate`) — три `COUNT(...) FILTER(...)` в одном
    запросе достаточно дёшевы, чтобы считать на каждый рендер виджета;
    материализация добавила бы Celery-таск + сигнал ради счётчика, который
    и так меняется только по прямому действию пользователя (в отличие от
    оценок, которые пересчитываются пачками после вайзарда).
    """
    row = MatchPrediction.objects.filter(match=match).aggregate(
        home=Count('id', filter=Q(choice=MatchPrediction.CHOICE_HOME)),
        draw=Count('id', filter=Q(choice=MatchPrediction.CHOICE_DRAW)),
        away=Count('id', filter=Q(choice=MatchPrediction.CHOICE_AWAY)),
    )
    total = row['home'] + row['draw'] + row['away']

    def pct(n: int) -> int:
        return round(n * 100 / total) if total else 0

    return {
        'home': row['home'], 'draw': row['draw'], 'away': row['away'],
        'total': total,
        'home_pct': pct(row['home']), 'draw_pct': pct(row['draw']), 'away_pct': pct(row['away']),
    }


def user_prediction(user, match) -> MatchPrediction | None:
    """Прогноз ИМЕННО этого пользователя — для подсветки его выбора и
    сверки "совпал/не совпал" после матча."""
    if not user or not user.is_authenticated:
        return None
    return MatchPrediction.objects.filter(match=match, user=user).first()


def bulk_prediction_data(matches, user) -> dict:
    """
    Bulk-версия prediction_counts()/user_prediction() выше — для встроенного
    компактного виджета прогноза прямо на карточке матча в списке
    (matches/views.py::MatchListView, задача "прогноз без перехода на
    страницу матча"). Без неё каждая карточка страницы делала бы свои
    2 запроса (agregate + прогноз пользователя) — до 40 лишних запросов на
    страницу из 20 матчей. Здесь на всю страницу — максимум 2 запроса
    суммарно, и только для матчей, где match.is_prediction_open() (обычно
    считанные единицы — окно прогноза всего 5 дней, см.
    Match.PREDICTION_WINDOW_DAYS).

    Возвращает {match.id: {'counts': dict, 'my_prediction': MatchPrediction|None}}
    — ключи ТОЛЬКО для матчей с открытым окном прогноза, остальные в списке
    просто не должны рисовать виджет (см. шаблон).
    """
    open_matches = [m for m in matches if m.is_prediction_open()]
    if not open_matches:
        return {}
    match_ids = [m.id for m in open_matches]

    counts_by_match = {
        m_id: {'home': 0, 'draw': 0, 'away': 0, 'total': 0, 'home_pct': 0, 'draw_pct': 0, 'away_pct': 0}
        for m_id in match_ids
    }
    rows = (
        MatchPrediction.objects.filter(match_id__in=match_ids)
        .values('match_id', 'choice')
        .annotate(n=Count('id'))
    )
    choice_key = {
        MatchPrediction.CHOICE_HOME: 'home',
        MatchPrediction.CHOICE_DRAW: 'draw',
        MatchPrediction.CHOICE_AWAY: 'away',
    }
    for row in rows:
        key = choice_key.get(row['choice'])
        if key:
            counts_by_match[row['match_id']][key] = row['n']
    for counts in counts_by_match.values():
        total = counts['home'] + counts['draw'] + counts['away']
        counts['total'] = total
        counts['home_pct'] = round(counts['home'] * 100 / total) if total else 0
        counts['draw_pct'] = round(counts['draw'] * 100 / total) if total else 0
        counts['away_pct'] = round(counts['away'] * 100 / total) if total else 0

    my_predictions = {}
    if user and user.is_authenticated:
        my_predictions = {
            p.match_id: p
            for p in MatchPrediction.objects.filter(match_id__in=match_ids, user=user)
        }

    return {
        m_id: {'counts': counts_by_match[m_id], 'my_prediction': my_predictions.get(m_id)}
        for m_id in match_ids
    }
