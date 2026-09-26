# evaluations/turning_points.py
"""Переломный момент матча: событие из ленты матча или универсальный вариант."""
from __future__ import annotations

from collections import Counter

# События, которые предлагаем как перелом.
EVENT_TYPES = ('goal', 'own_goal', 'penalty', 'red_card', 'disallowed_goal', 'var_check')
EVENT_ICONS = {
    'goal': 'ti-ball-football', 'own_goal': 'ti-ball-football', 'penalty': 'ti-target-arrow',
    'red_card': 'ti-rectangle-vertical', 'disallowed_goal': 'ti-ban', 'var_check': 'ti-device-tv',
}

# Универсальные варианты: (ключ, подпись, иконка).
KINDS = (
    ('referee_decision', 'Решение судьи', 'ti-cards'),
    ('substitution', 'Удачная замена', 'ti-replace'),
    ('tactics', 'Смена тактики', 'ti-chess-knight'),
    ('save', 'Сейв вратаря', 'ti-hand-stop'),
    ('missed_chance', 'Упущенный момент', 'ti-target-off'),
    ('injury', 'Травма игрока', 'ti-first-aid-kit'),
    ('momentum', 'Перелом по игре', 'ti-trending-up'),
    ('other', 'Другое', 'ti-dots'),
)
KIND_LABELS = {key: label for key, label, _icon in KINDS}
KIND_CHOICES = [(key, label) for key, label, _icon in KINDS]

# Топ вариантов в агрегате матча.
TOP_LIMIT = 3


def match_events(match):
    """События матча, которые можно назвать переломом, по хронологии."""
    return (
        match.events.filter(event_type__in=EVENT_TYPES)
        .select_related('player').order_by('minute', 'added_time', 'id')
    )


def event_label(event) -> str:
    """«Гол 90+5' · Ерланов»."""
    name = event.player_display_name
    base = f"{event.get_event_type_display()} {event.display_minute}'"
    return f"{base} · {name}" if name else base


def resolve_choice(value: str | None, match):
    """Разбор значения радио «event:<id>» / «kind:<key>» -> (event | None, kind). Чужое — игнор."""
    value = (value or '').strip()
    if value.startswith('event:'):
        event = match_events(match).filter(id=value[6:]).first() if _is_uuid(value[6:]) else None
        return event, ''
    if value.startswith('kind:') and value[5:] in KIND_LABELS:
        return None, value[5:]
    return None, ''


def top_turning_points(evaluations) -> list[dict]:
    """Топ названных переломов среди оценок с turning_point: [{key, label, icon, count, pct}].
    pct — от тех, кто уточнил, что именно было переломом.
    """
    counts: Counter = Counter()
    events = {}
    for evaluation in evaluations:
        if not evaluation.turning_point:
            continue
        if evaluation.turning_point_event_id:
            key = f"event:{evaluation.turning_point_event_id}"
            events[key] = evaluation.turning_point_event
        elif evaluation.turning_point_kind:
            key = f"kind:{evaluation.turning_point_kind}"
        else:
            continue
        counts[key] += 1

    total = sum(counts.values())
    result = []
    for key, count in counts.most_common(TOP_LIMIT):
        if key.startswith('event:'):
            event = events[key]
            label, icon = event_label(event), EVENT_ICONS.get(event.event_type, 'ti-bolt')
        else:
            kind = key[5:]
            label = KIND_LABELS.get(kind, kind)
            icon = next((i for k, _l, i in KINDS if k == kind), 'ti-bolt')
        result.append({'key': key, 'label': label, 'icon': icon, 'count': count, 'pct': round(count * 100 / total)})
    return result


def _is_uuid(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True
