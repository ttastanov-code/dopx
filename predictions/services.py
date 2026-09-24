# predictions/services.py
"""Сервисный слой прогнозов 1X2."""
from __future__ import annotations

from django.db.models import Count, Q

from .models import MatchPrediction


def submit_prediction(*, user, match, choice: str) -> tuple[MatchPrediction, bool] | tuple[None, bool]:
    """Ставит или меняет прогноз. Снять прогноз нельзя, повторный выбор — no-op.
    Окно проверяется здесь (POST можно отправить в обход UI).
    Возвращает (prediction, created) или (None, False), если окно закрыто.
    User и Celery не трогает — это делает views.py.
    """
    if not match.is_prediction_open():
        return None, False
    prediction, created = MatchPrediction.objects.update_or_create(
        match=match, user=user, defaults={'choice': choice},
    )
    return prediction, created


def prediction_counts(match) -> dict:
    """Доли голосов по трём исходам одним запросом, проценты округлены в Python."""
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
    """Прогноз текущего пользователя."""
    if not user or not user.is_authenticated:
        return None
    return MatchPrediction.objects.filter(match=match, user=user).first()


def bulk_prediction_data(matches, user) -> dict:
    """Bulk prediction_counts + user_prediction для карточек списка — максимум 2 запроса.
    {match.id: {'counts': dict, 'my_prediction': MatchPrediction|None}} — только матчи с открытым окном.
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


def bulk_final_prediction_counts(match_ids) -> dict:
    """Bulk prediction_counts без проверки окна — для завершённых матчей (индекс сенсации).

    :param match_ids: id матчей.
    :return: {match_id: counts_dict}; матча без прогнозов в словаре нет.
    """
    match_ids = list(match_ids)
    if not match_ids:
        return {}

    counts_by_match: dict = {}
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
        if not key:
            continue
        entry = counts_by_match.setdefault(
            row['match_id'], {'home': 0, 'draw': 0, 'away': 0}
        )
        entry[key] = row['n']

    result = {}
    for match_id, counts in counts_by_match.items():
        total = counts['home'] + counts['draw'] + counts['away']
        result[match_id] = {
            'home': counts['home'], 'draw': counts['draw'], 'away': counts['away'],
            'total': total,
            'home_pct': round(counts['home'] * 100 / total) if total else 0,
            'draw_pct': round(counts['draw'] * 100 / total) if total else 0,
            'away_pct': round(counts['away'] * 100 / total) if total else 0,
        }
    return result
