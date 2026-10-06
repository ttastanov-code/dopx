"""Лента событий матча: периоды, ход счёта, сводка по командам и сверка с итоговым счётом."""

GOAL_TYPES = ('goal', 'penalty', 'own_goal')

# Тип -> (подпись, css-модификатор). Порядок — порядок строк в сводке.
KINDS = {
    'goal': ('Гол', 'goal'),
    'penalty': ('Гол с пенальти', 'goal'),
    'own_goal': ('Автогол', 'own'),
    'missed_penalty': ('Пенальти не забит', 'miss'),
    'disallowed_goal': ('Гол отменён', 'void'),
    'var_check': ('VAR', 'var'),
    'yellow_card': ('Жёлтая карточка', 'yellow'),
    'red_card': ('Красная карточка', 'red'),
    'substitution': ('Замена', 'sub'),
}

# Причины карточек по-человечески (в choices модели «Диссидентство» — калька с dissent).
CARD_REASONS = {
    'unsporting': 'Неспортивное поведение',
    'dissent': 'Пререкания с судьёй',
    'persistent_fouling': 'Систематические нарушения',
    'delaying_restart': 'Затягивание времени',
    'entering_field': 'Выход на поле без разрешения',
}

SUMMARY_ROWS = (
    ('goals', 'Голы', 'goal', GOAL_TYPES),
    ('yellow', 'Жёлтые', 'yellow', ('yellow_card',)),
    ('red', 'Красные', 'red', ('red_card',)),
    ('subs', 'Замены', 'sub', ('substitution',)),
    ('var', 'VAR', 'var', ('var_check', 'disallowed_goal')),
)


def _period(e) -> int:
    """1 — первый тайм (45+x тоже), 2 — второй, 3 — овертайм."""
    if e.minute <= 45:
        return 1
    if e.minute <= 90:
        return 2
    return 3


def build_timeline(match, events) -> dict:
    """Строки ленты с разделителями таймов, счётом после каждого гола и сводкой."""
    events = list(events)
    home = away = 0
    rows = []
    started = bool(events) or match.status in ('live', 'finished')
    if started:
        rows.append({'divider': 'Начало матча'})
    period = 1
    ht_score = None

    def cross(to):
        nonlocal ht_score
        if to == 2:
            ht_score = (home, away)
            rows.append({'divider': 'Перерыв', 'score': f'{home}:{away}'})
        elif to == 3:
            rows.append({'divider': 'Дополнительное время', 'score': f'{home}:{away}'})

    for e in events:
        p = _period(e)
        while period < p:
            period += 1
            cross(period)
        label, mod = KINDS.get(e.event_type, (e.get_event_type_display(), 'other'))
        score = None
        if e.event_type in GOAL_TYPES:
            if e.team_side == 'home':
                home += 1
            else:
                away += 1
            score = f'{home}:{away}'
        reason = CARD_REASONS.get(e.card_reason or '') if mod in ('yellow', 'red') else None
        rows.append({'event': e, 'label': label, 'mod': mod, 'score': score, 'side': e.team_side,
                     'reason': reason})

    real = (match.home_score or 0, match.away_score or 0)
    if match.status == 'finished':
        if period == 1:
            cross(2)
        rows.append({'divider': 'Финальный свисток', 'score': f'{real[0]}:{real[1]}', 'final': True})

    summary = []
    for key, title, mod, types in SUMMARY_ROWS:
        h = sum(1 for e in events if e.event_type in types and e.team_side == 'home')
        a = sum(1 for e in events if e.event_type in types and e.team_side == 'away')
        if h or a or key == 'goals':
            total = h + a or 1
            summary.append({'title': title, 'mod': mod, 'home': h, 'away': a,
                            'home_pct': round(h * 100 / total), 'away_pct': round(a * 100 / total)})

    # Голы в ленте должны сходиться со счётом матча — иначе честно предупреждаем.
    mismatch = match.status in ('live', 'finished') and (home, away) != real
    return {'rows': rows, 'summary': summary, 'mismatch': mismatch, 'ht_score': ht_score,
            'has_events': bool(events)}
