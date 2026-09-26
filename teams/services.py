# teams/services.py
"""Сервисы команды: индекс настроения клуба, форма, спорные матчи сезона,
таблица «до/после» конкретного матча. Всё считается на лету из уже
посчитанных агрегатов.
"""
from __future__ import annotations

import math
from collections import defaultdict

from django.core.cache import cache
from django.db.models import Count, Q

from aggregates.models import RefereeMatchAggregate, TeamMatchAggregate
from aggregates.services import published_q

# Сколько последних матчей берём для тренда.
MOOD_TREND_RECENT_MATCHES = 5

# Разница меньше этого — шум, а не тренд.
MOOD_TREND_NOISE_THRESHOLD = 0.3

# Кэш на час.
MOOD_TREND_CACHE_TIMEOUT = 3600


def compute_mood_trend(team) -> dict | None:
    """Тренд настроения по последним матчам.
    :return: None, если матчей меньше 2, иначе {direction, delta, matches_considered}.
    """
    cache_key = f"team_mood_trend_{team.id}"
    cached = cache.get(cache_key, "__unset__")
    if cached != "__unset__":
        return cached

    recent = list(
        TeamMatchAggregate.objects.filter(published_q(), team=team, total_votes__gt=0)
        .order_by("-match__start_time")[:MOOD_TREND_RECENT_MATCHES]
    )
    if len(recent) < 2:
        cache.set(cache_key, None, MOOD_TREND_CACHE_TIMEOUT)
        return None

    # Сравниваем свежую и старую половины окна.
    midpoint = max(len(recent) // 2, 1)
    newer_half = recent[:midpoint]
    older_half = recent[midpoint:] or recent[midpoint - 1:]
    newer_avg = sum(r.performance_score for r in newer_half) / len(newer_half)
    older_avg = sum(r.performance_score for r in older_half) / len(older_half)
    delta = round(newer_avg - older_avg, 1)

    if delta >= MOOD_TREND_NOISE_THRESHOLD:
        direction = "up"
    elif delta <= -MOOD_TREND_NOISE_THRESHOLD:
        direction = "down"
    else:
        direction = "flat"

    result = {"direction": direction, "delta": delta, "matches_considered": len(recent)}
    cache.set(cache_key, result, MOOD_TREND_CACHE_TIMEOUT)
    return result


# ---------------------------------------------------------------------------
# Индекс настроения клуба v2: ряды по матчам — доверие своих фанатов
# (own_fans_avg), общий рейтинг и ожидания перед матчем (доля прогнозов на победу).
# ---------------------------------------------------------------------------

# Глубина графика.
MOOD_SERIES_MATCHES = 10

MOOD_SERIES_CACHE_TIMEOUT = 3600

# Порог расхождения оценок судейства для «спорных решений сезона».
CONTROVERSIAL_GAP_THRESHOLD = 1.5


def compute_mood_series(team, *, limit: int = MOOD_SERIES_MATCHES) -> list[dict]:
    """Ряд по матчам для графика настроения клуба:
    - trust — own_fans_avg (только свои фанаты);
    - mood — performance_score (все зрители);
    - expectation_pct — доля прогнозов на победу команды до матча (None, если прогнозов нет).

    :return: список dict в хронологическом порядке.
    """
    cache_key = f"team_mood_series_{team.id}_{limit}"
    cached = cache.get(cache_key, "__unset__")
    if cached != "__unset__":
        return cached

    aggs = list(
        TeamMatchAggregate.objects.filter(published_q(), team=team, total_votes__gt=0)
        .select_related("match", "match__home_team", "match__away_team")
        .order_by("-match__start_time")[:limit]
    )
    aggs.reverse()  # хронологически

    from predictions.models import MatchPrediction

    match_ids = [agg.match_id for agg in aggs]
    prediction_counts: dict = defaultdict(lambda: defaultdict(int))
    for row in (
        MatchPrediction.objects.filter(match_id__in=match_ids)
        .values("match_id", "choice").annotate(n=Count("id"))
    ):
        prediction_counts[row["match_id"]][row["choice"]] = row["n"]

    points = []
    for agg in aggs:
        match = agg.match
        is_home = match.home_team_id == team.id
        opponent = match.away_team if is_home else match.home_team
        win_choice = MatchPrediction.CHOICE_HOME if is_home else MatchPrediction.CHOICE_AWAY
        counts = prediction_counts.get(match.id, {})
        total_predictions = sum(counts.values())
        expectation_pct = (
            round(counts.get(win_choice, 0) / total_predictions * 100, 1) if total_predictions else None
        )
        points.append({
            "match_id": str(match.id),
            "label": f"{match.start_time:%d.%m}",
            "opponent": opponent.name if opponent else "",
            "trust": agg.own_fans_avg,
            "mood": agg.performance_score,
            "expectation_pct": expectation_pct,
            "total_predictions": total_predictions,
        })

    cache.set(cache_key, points, MOOD_SERIES_CACHE_TIMEOUT)
    return points


def build_sparkline_points(
    series: list[dict], key: str, *, value_max: float = 10.0,
    width: int = 560, height: int = 120, pad_x: int = 12, pad_y: int = 14,
) -> str:
    """Строка points для SVG <polyline>. Точки с None пропускаются.
    Пустая строка, если валидных точек меньше 2.
    """
    n = len(series)
    if n < 2:
        return ""
    step = (width - 2 * pad_x) / max(n - 1, 1)

    def scale_y(value: float) -> float:
        clamped = max(0.0, min(value_max, value))
        return height - pad_y - (clamped / value_max) * (height - 2 * pad_y)

    coords = [
        (pad_x + i * step, scale_y(point[key]))
        for i, point in enumerate(series) if point.get(key) is not None
    ]
    if len(coords) < 2:
        return ""
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)


# Геометрия графика настроения: реакция и доверие на общей шкале, считается на сервере.
MOOD_CHART_WIDTH = 640
MOOD_MAIN_HEIGHT = 160
# Отступы внутри SVG: сверху/снизу — чтобы точки на краях шкалы не обрезались.
MOOD_CHART_PAD_X = 0
MOOD_CHART_PAD_TOP = 12
MOOD_CHART_PAD_BOTTOM = 12
MOOD_CHART_VALUE_MAX = 10.0
# Минимальный размах шкалы: иначе шум в 0.2 балла выглядит обвалом.
MOOD_CHART_MIN_SPAN = 4

EXPECTATION_BAR_WIDTH = 30
EXPECTATION_BAR_GAP = 16
EXPECTATION_BAR_MAX_H = 40
# Высота с запасом под подпись даты (иначе она обрезается viewBox).
EXPECTATION_CHART_HEIGHT = 84
EXPECTATION_BASELINE_Y = 56

# Секцию «Ожидания перед матчем» показываем при любой точке с данными.
EXPECTATION_MIN_COVERAGE_COUNT = 1
EXPECTATION_MIN_COVERAGE_RATIO = 0.0


def _build_series_chart(
    series: list[dict], key: str, *,
    width: int, height: int, pad_x: int, pad_top: int, pad_bottom: int,
    value_min: float = 0.0, value_max: float = MOOD_CHART_VALUE_MAX, grid_values: tuple = (0, 5, 10),
) -> dict:
    """Геометрия одного мини-графика. latest/latest_point — последняя известная
    точка ряда (даже если рисовать линию не из чего).
    """
    plot_h = height - pad_top - pad_bottom
    n = len(series)
    step = (width - 2 * pad_x) / max(n - 1, 1)
    baseline_y = pad_top + plot_h

    def scale_y(value: float) -> float:
        clamped = max(value_min, min(value_max, value))
        return pad_top + plot_h - ((clamped - value_min) / (value_max - value_min)) * plot_h

    gridlines = [{"y": round(scale_y(v), 1), "label": f"{v:g}"} for v in grid_values]

    latest = None
    latest_point = None
    for point in reversed(series):
        if point.get(key) is not None:
            latest = point[key]
            latest_point = point
            break

    pts = [
        (pad_x + i * step, scale_y(point[key]), point)
        for i, point in enumerate(series) if point.get(key) is not None
    ]
    result = {
        "has_data": len(pts) >= 2,
        "width": width, "height": height,
        "gridlines": gridlines, "baseline_y": round(baseline_y, 1),
        "latest": latest, "latest_point": latest_point,
    }
    if len(pts) < 2:
        return result

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
    area_cmds = [f"M {pts[0][0]:.1f},{baseline_y:.1f}"]
    area_cmds += [f"L {x:.1f},{y:.1f}" for x, y, _ in pts]
    area_cmds.append(f"L {pts[-1][0]:.1f},{baseline_y:.1f} Z")
    dots = [
        {"x": round(x, 1), "y": round(y, 1), "value": point[key], "label": point["label"],
         "opponent": point["opponent"], "trust": point.get("trust"), "is_last": point is series[-1]}
        for x, y, point in pts
    ]
    result["polyline"] = polyline
    result["area_path"] = " ".join(area_cmds)
    result["dots"] = dots
    return result


def _build_expectation_bars(series: list[dict]) -> dict:
    """SVG-бары «Ожидания перед матчем». Матч без прогнозов — тире на базовой линии, а не 0%."""
    unit = EXPECTATION_BAR_WIDTH + EXPECTATION_BAR_GAP
    width = round(len(series) * unit - EXPECTATION_BAR_GAP, 1) if series else 0
    bars = []
    for i, point in enumerate(series):
        x = i * unit
        cx = round(x + EXPECTATION_BAR_WIDTH / 2, 1)
        pct = point["expectation_pct"]
        if pct is None:
            bars.append({
                "has_data": False, "x": round(x, 1), "cx": cx,
                "width": EXPECTATION_BAR_WIDTH,  # нужен для чёрточки
                "label": point["label"], "opponent": point["opponent"],
            })
            continue
        bar_h = max(round(EXPECTATION_BAR_MAX_H * max(0.0, min(100.0, pct)) / 100, 1), 2)
        bars.append({
            "has_data": True, "x": round(x, 1), "cx": cx,
            "y": round(EXPECTATION_BASELINE_Y - bar_h, 1),
            "width": EXPECTATION_BAR_WIDTH, "height": bar_h,
            "pct": pct, "label": point["label"], "opponent": point["opponent"],
        })
    return {
        "width": width, "height": EXPECTATION_CHART_HEIGHT,
        "baseline_y": EXPECTATION_BASELINE_Y, "bars": bars,
    }


def _mood_domain(series: list[dict]) -> tuple[int, int]:
    """Целочисленная шкала по данным (±1 балл запаса) в пределах 0–10, размах не меньше MOOD_CHART_MIN_SPAN."""
    values = [p[k] for p in series for k in ("mood", "trust") if p.get(k) is not None]
    if not values:
        return 0, int(MOOD_CHART_VALUE_MAX)
    top = int(MOOD_CHART_VALUE_MAX)
    lo = max(0, math.floor(min(values)) - 1)
    hi = min(top, math.ceil(max(values)) + 1)
    while hi - lo < MOOD_CHART_MIN_SPAN:
        if lo > 0:
            lo -= 1
        if hi - lo < MOOD_CHART_MIN_SPAN and hi < top:
            hi += 1
    return lo, hi


def build_mood_chart(series: list[dict]) -> dict | None:
    """Данные для графика настроения клуба. None, если оценённых матчей нет."""
    if not series:
        return None

    lo, hi = _mood_domain(series)
    geometry = dict(
        width=MOOD_CHART_WIDTH, height=MOOD_MAIN_HEIGHT,
        pad_x=MOOD_CHART_PAD_X, pad_top=MOOD_CHART_PAD_TOP, pad_bottom=MOOD_CHART_PAD_BOTTOM,
        value_min=lo, value_max=hi, grid_values=(lo, round((lo + hi) / 2), hi),
    )
    mood = _build_series_chart(series, "mood", **geometry)
    # Та же шкала: линия доверия накладывается на общий график.
    trust = _build_series_chart(series, "trust", **geometry)
    expectation = _build_expectation_bars(series)
    expectation_covered = sum(1 for point in series if point["expectation_pct"] is not None)
    has_expectation_data = (
        expectation_covered >= EXPECTATION_MIN_COVERAGE_COUNT
        and expectation_covered / len(series) >= EXPECTATION_MIN_COVERAGE_RATIO
    )

    expectations = [point["expectation_pct"] for point in series if point["expectation_pct"] is not None]
    # Подписи оси X: соперник и дата — первые/последние, середину пропускаем при тесноте.
    axis = [{"opponent": point["opponent"], "label": point["label"]} for point in series]
    return {
        "mood": mood,
        "trust": trust,
        "expectation": expectation,
        "avg_expectation": round(sum(expectations) / len(expectations)) if expectations else None,
        "axis": axis,
        "has_expectation_data": has_expectation_data,
        "latest_mood": mood["latest"],
        "latest_trust": trust["latest"],
        "match_count": len(series),
        "oldest_label": f"{series[0]['opponent']} {series[0]['label']}",
        "newest_label": f"{series[-1]['opponent']} {series[-1]['label']}",
    }


def find_season_controversial_matches(team, season, *, limit: int = 3) -> list[dict]:
    """Самые спорные матчи сезона по расхождению оценок судейства фанатами
    хозяев и гостей. Это разброс мнений, а не факт судейской ошибки.
    """
    aggs = list(
        RefereeMatchAggregate.objects.filter(published_q(), match__season=season)
        .filter(Q(match__home_team=team) | Q(match__away_team=team))
        .select_related("match", "match__home_team", "match__away_team")
    )
    scored = []
    for agg in aggs:
        if agg.home_fans_avg is None or agg.away_fans_avg is None:
            continue
        gap = abs(agg.home_fans_avg - agg.away_fans_avg)
        if gap < CONTROVERSIAL_GAP_THRESHOLD:
            continue
        scored.append((gap, agg))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    result = []
    for gap, agg in scored[:limit]:
        match = agg.match
        result.append({
            "match": match,
            "gap": round(gap, 1),
            "label": f"{match.home_team.name} {match.home_score}:{match.away_score} {match.away_team.name}",
            "date": match.start_time,
        })
    return result


# Сколько последних матчей в форме команды.
TEAM_FORM_RECENT_MATCHES = 5


def _build_form_entries(team, matches) -> list[dict]:
    """W/D/L-записи по списку матчей (порядок как на входе)."""
    form = []
    for match in matches:
        if match.home_score is None or match.away_score is None:
            continue
        is_home = match.home_team_id == team.id
        own_score = match.home_score if is_home else match.away_score
        opp_score = match.away_score if is_home else match.home_score
        opponent = match.away_team if is_home else match.home_team

        if own_score > opp_score:
            result = "W"
        elif own_score < opp_score:
            result = "L"
        else:
            result = "D"

        form.append({
            "result": result,
            "match": match,
            "opponent": opponent,
            "is_home": is_home,
            "score_display": f"{own_score}:{opp_score}",
        })
    return form


def get_team_form(team, matches) -> list[dict]:
    """Форма команды из уже полученного списка матчей (новые сначала).

    :return: [{result, match, opponent, score_display}] от старых к новым.
    """
    form = _build_form_entries(team, list(matches)[:TEAM_FORM_RECENT_MATCHES])
    form.reverse()
    return form


def describe_form_streak(form: list[dict]) -> str | None:
    """Серия одной фразой: «4 победы подряд» / «не побеждает 5 матчей».
    None, если данных нет или серии нет.
    """
    if not form:
        return None

    recent = list(reversed(form))
    latest_result = recent[0]['result']
    is_win_streak = latest_result == 'W'

    streak_len = 0
    for entry in recent:
        entry_is_win = entry['result'] == 'W'
        if entry_is_win == is_win_streak:
            streak_len += 1
        else:
            break

    def _pluralize_matches(n: int) -> str:
        if n % 10 == 1 and n % 100 != 11:
            return 'матч'
        if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
            return 'матча'
        return 'матчей'

    if is_win_streak:
        if streak_len < 2:
            return 'Победа в последнем матче'
        return f'{streak_len} побед{_win_suffix(streak_len)} подряд'

    if streak_len < 2:
        return None  # одиночный результат — не серия
    return f'Не побеждает {streak_len} {_pluralize_matches(streak_len)}'


def describe_season_form_streak(team, season_matches) -> str | None:
    """Та же фраза о серии, но по всем матчам сезона (без среза до 5).

    :param season_matches: завершённые матчи сезона, новые сначала.
    """
    form = _build_form_entries(team, season_matches)
    form.reverse()
    return describe_form_streak(form)


def _win_suffix(n: int) -> str:
    """Склонение слова «победа»."""
    if n % 10 == 1 and n % 100 != 11:
        return 'а'
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return 'ы'
    return ''


# ---------------------------------------------------------------------------
# Таблица на момент конкретного матча (для бейджа «Изменил таблицу» и виджета
# на странице матча). Считается на лету тем же алгоритмом, что и реальная
# таблица, только со срезом по дате.
# ---------------------------------------------------------------------------
def _standings_stats_asof(season, cutoff) -> dict:
    """{team_id: {points, goal_diff, goals_scored}} по матчам строго до cutoff."""
    from teams.models import TeamSeason

    team_ids = list(
        TeamSeason.objects.filter(season=season).values_list('team_id', flat=True)
    )
    stats_by_team = {tid: {'points': 0, 'goal_diff': 0, 'goals_scored': 0} for tid in team_ids}

    from matches.models import Match

    rows = Match.objects.filter(
        season=season, status='finished', start_time__lt=cutoff,
    ).values('home_team_id', 'away_team_id', 'home_score', 'away_score')

    for row in rows:
        _apply_match_result_to_stats(
            stats_by_team, row['home_team_id'], row['home_score'], row['away_team_id'], row['away_score'],
        )
    return stats_by_team


def _apply_match_result_to_stats(stats_by_team, home_id, home_score, away_id, away_score) -> None:
    if home_score is None or away_score is None:
        return
    if home_id in stats_by_team:
        s = stats_by_team[home_id]
        s['goals_scored'] += home_score
        s['goal_diff'] += home_score - away_score
        if home_score > away_score:
            s['points'] += 3
        elif home_score == away_score:
            s['points'] += 1
    if away_id in stats_by_team:
        s = stats_by_team[away_id]
        s['goals_scored'] += away_score
        s['goal_diff'] += away_score - home_score
        if away_score > home_score:
            s['points'] += 3
        elif away_score == home_score:
            s['points'] += 1


def _rank_standings(stats_by_team) -> dict:
    ordered = sorted(
        stats_by_team.items(),
        key=lambda kv: (-kv[1]['points'], -kv[1]['goal_diff'], -kv[1]['goals_scored']),
    )
    return {team_id: position for position, (team_id, _stats) in enumerate(ordered, start=1)}


def compute_standings_asof(season, cutoff) -> dict:
    """Позиции команд сезона по матчам строго до cutoff.
    :return: {team_id: position}; команды без матчей тоже включены.
    """
    return _rank_standings(_standings_stats_asof(season, cutoff))


def compute_match_table_impact_positions(match) -> tuple[dict, dict]:
    """Позиции до и сразу после конкретного матча (один запрос к БД).
    :return: (positions_before, positions_after)
    """
    stats_before = _standings_stats_asof(match.season, match.start_time)
    positions_before = _rank_standings(stats_before)

    stats_after = {tid: dict(s) for tid, s in stats_before.items()}
    if match.status == 'finished':
        _apply_match_result_to_stats(
            stats_after, match.home_team_id, match.home_score, match.away_team_id, match.away_score,
        )
    positions_after = _rank_standings(stats_after)

    return positions_before, positions_after


def get_pre_match_standings_snapshot(match) -> dict | None:
    """Таблица перед матчем для виджета на странице матча.
    None, если до матча в сезоне ещё не было игр.
    """
    from matches.models import Match

    has_prior_matches = Match.objects.filter(
        season=match.season, status='finished', start_time__lt=match.start_time,
    ).exists()
    if not has_prior_matches:
        return None

    stats = _standings_stats_asof(match.season, match.start_time)
    positions = _rank_standings(stats)
    total_teams = len(stats)

    home_stats = stats.get(match.home_team_id)
    away_stats = stats.get(match.away_team_id)
    if home_stats is None or away_stats is None:
        return None

    def _row(team, team_stats):
        return {
            'team': team,
            'position': positions.get(team.id),
            'points': team_stats['points'],
            'goal_diff': team_stats['goal_diff'],
        }

    return {
        'home': _row(match.home_team, home_stats),
        'away': _row(match.away_team, away_stats),
        'total_teams': total_teams,
    }
