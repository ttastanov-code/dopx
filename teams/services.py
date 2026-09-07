# teams/services.py
"""
Индекс настроения клуба — MVP (docs/PRODUCT_SCOPE_MATCH_DNA_AND_EXPLAINABILITY.md,
раздел 3, docs/adr/0029-club-mood-index-mvp.md). Сознательно НЕ полноценный
time-series индекс с историей: ни новой модели, ни Celery-задачи — только
тренд-бейдж (↑/↓/→), считаемый на лету из уже существующего
`TeamMatchAggregate` (его `Meta.ordering = ['-match__start_time']`, см.
aggregates/models.py, поэтому "последние N матчей" — это просто первые N
строк без доп. сортировки). Полноценная версия — только если этот дешёвый
сигнал подтвердит интерес пользователей (см. "Риск" в scope-документе:
психологическая метрика "настроение" достаточно спекулятивна, чтобы не
проектировать сразу финальную схему хранения).
"""
from __future__ import annotations

from collections import defaultdict

from django.core.cache import cache
from django.db.models import Count, Q

from aggregates.models import RefereeMatchAggregate, TeamMatchAggregate

# Сколько последних матчей смотрим — то же число, что и в MVP-предложении
# scope-документа.
MOOD_TREND_RECENT_MATCHES = 5

# Порог в баллах performance_score, ниже которого разницу двух половин окна
# считаем шумом, а не трендом — иначе даже случайные ±0.1 давали бы стрелку
# вверх/вниз на каждый чих.
MOOD_TREND_NOISE_THRESHOLD = 0.3

# 1 час — агрегаты и так пересчитываются асинхронно (aggregates/tasks.py) не
# мгновенно после каждого голоса, реалтайм тут не нужен; кэш нужен, чтобы не
# гонять этот запрос на каждый рендер страницы команды.
MOOD_TREND_CACHE_TIMEOUT = 3600


def compute_mood_trend(team) -> dict | None:
    """:return: None, если данных меньше 2 матчей (сравнивать не с чем —
        тот же принцип "меньше 2 сегментов" из bias_segment_text), иначе
        dict {direction: 'up'|'down'|'flat', delta: float, matches_considered: int}.
    """
    cache_key = f"team_mood_trend_{team.id}"
    cached = cache.get(cache_key, "__unset__")
    if cached != "__unset__":
        return cached

    recent = list(
        TeamMatchAggregate.objects.filter(team=team, total_votes__gt=0)
        .order_by("-match__start_time")[:MOOD_TREND_RECENT_MATCHES]
    )
    if len(recent) < 2:
        cache.set(cache_key, None, MOOD_TREND_CACHE_TIMEOUT)
        return None

    # Сравниваем более свежую и более старую половины окна (не просто
    # "первый минус последний") — один выброс на краю окна не должен в
    # одиночку решать направление тренда.
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
# Индекс настроения клуба v2 (docs/adr/0034-club-mood-index-v2.md) —
# настоящий time-series (не одно число), плюс отдельная метрика "доверие
# болельщиков" (own_fans_avg, а не общий performance_score — общий рейтинг
# смешивает мнение своих и чужих фанатов, "доверие" должно быть только про
# своих), плюс связка с уже существующим приложением `predictions`
# ("ожидания перед матчем" — не отдельная новая метрика, а переиспользование
# 1X2-прогнозов, которые пользователи и так оставляют ДО матча).
#
# По-прежнему НЕ отдельная модель/Celery-задача — тот же принцип "считаем на
# лету из уже посчитанного", что и v1 (compute_mood_trend выше), только
# глубже (10 матчей вместо 5) и раскладывается на несколько параллельных
# рядов вместо одного числа-дельты.
# ---------------------------------------------------------------------------

# Глубина графика — больше, чем у бейджа тренда (MOOD_TREND_RECENT_MATCHES=5):
# на карточке-бейдже важна свежая динамика, на графике — общая картина сезона.
MOOD_SERIES_MATCHES = 10

MOOD_SERIES_CACHE_TIMEOUT = 3600

# Порог |home_fans_avg - away_fans_avg| у RefereeMatchAggregate, начиная с
# которого матч считаем "спорным решением сезона" — тот же порог и тот же
# смысл, что matches/services.py::REFEREE_DIVERGENCE_MIN_GAP. НЕ импортируем
# оттуда: matches/ и teams/ не должны знать друг о друге ради одной
# константы (тот же принцип, что уже применён к NOTABLE_EVENT_TYPES между
# round_squad/season_squad).
CONTROVERSIAL_GAP_THRESHOLD = 1.5


def compute_mood_series(team, *, limit: int = MOOD_SERIES_MATCHES) -> list[dict]:
    """Per-матчевый ряд для графика "Индекс настроения клуба" — три
    параллельные метрики на общей временнОй оси (индекс совпадает с позицией
    матча в списке, а не с датой — так пропуски в одном ряду, например матч
    без прогнозов, не сдвигают точки другого ряда):

    - trust — own_fans_avg команды за матч (TeamMatchAggregate) — ТОЛЬКО
      мнение своих же фанатов, "доверие", а не общий рейтинг матча.
    - mood — performance_score команды за матч — общая "реакция после
      матча" всех зрителей, свои+чужие+нейтралы вместе.
    - expectation_pct — доля прогнозов на победу ЭТОЙ команды среди всех
      прогнозов (`predictions.MatchPrediction`) на этот матч, ДО игры —
      "ожидания перед матчем". `None`, если прогнозов на матч не было
      вообще (не 0% — 0% подразумевало бы, что кто-то прогнозировал и все
      ошиблись, а "никто не прогнозировал" это другое состояние).

    :return: список dict (может быть пустым), в хронологическом порядке
        (старые матчи слева, свежие справа — порядок графика, а не БД).
    """
    cache_key = f"team_mood_series_{team.id}_{limit}"
    cached = cache.get(cache_key, "__unset__")
    if cached != "__unset__":
        return cached

    aggs = list(
        TeamMatchAggregate.objects.filter(team=team, total_votes__gt=0)
        .select_related("match", "match__home_team", "match__away_team")
        .order_by("-match__start_time")[:limit]
    )
    aggs.reverse()  # хронологически, для графика слева-направо

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
    """Готовая строка для атрибута `points` SVG `<polyline>` — весь расчёт
    координат на сервере, БЕЗ клиентской библиотеки графиков (ADR-0025 убрал
    рантайм-зависимость от внешних CDN, новый JS-чарт нарушил бы это решение
    заново). x — позиция матча в `series` (не дата — см. докстринг
    compute_mood_series), y — линейное отображение значения в [0, value_max]
    на высоту SVG (0 внизу, value_max вверху).

    Точки с `None` (например, expectation_pct на матч без прогнозов) просто
    пропускаются — сохраняются ПРАВИЛЬНЫЕ x-координаты вокруг пропуска
    (одна ломаная разбивается на видимые линию, если смотреть на две руки
    от разрыва), а не искажается позиция остальных точек. :return: пустая
    строка, если валидных точек меньше 2 — рисовать нечего.
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


# Геометрия премиального графика "Индекс настроения клуба"
# (build_mood_chart, ниже) — ПЕРЕСОБРАНО 2026-09-07 второй раз, продуктовый
# фидбек на живом скрине первой версии: "график уродский, синий и зелёный
# сливаются", подписи сетки слева читались как обрывки цифр ("1","7","5","2",
# "0" вместо "10"/"7.5"/"5"/"2.5"/"0" — узкий левый паддинг + десятичные дроби
# в подписях были на грани обрезания при масштабировании viewBox под ширину
# карточки), и "Доверие сейчас" показывало пустое число ("/10" без цифры —
# `latest_trust` брался как `series[-1]["trust"]` НАПРЯМУЮ, а не как
# последнее РЕАЛЬНО известное значение; если у самого свежего матча доверия
# не было, а у более раннего было — блок был truthy, а число внутри пустое).
#
# Что изменилось концептуально, не только косметически:
#   1. mood и trust больше НЕ делят один график — это и была причина
#      "сливаются": два полупрозрачных градиента друг на друге на одной оси
#      неотличимы, если значения близки. Теперь это два независимых мини-
#      графика (`_build_series_chart`, вызывается дважды с разными width/
#      height) — mood крупный "герой" сверху, trust компактный приглушённый
#      снизу, у каждого своя сетка и свой baseline.
#   2. Сетка — только "круглые" 0/5/10 (MOOD_CHART_GRID_VALUES), без 2.5/7.5.
#      Не только эстетика: целые числа физически не могут "обрезаться"
#      десятичной точкой при любом масштабировании, в отличие от "7.5".
#   3. Левый паддинг (MOOD_CHART_PAD_X) увеличен настолько, чтобы подпись
#      "10" гарантированно помещалась ДО начала графика при `text-anchor:
#      end` — подпись растёт ВЛЕВО от фиксированной точки, а не вправо
#      поверх графика, поэтому обрезаться ей физически не обо что.
#   4. "Ожидания перед матчем" — БЫЛИ HTML/flex-блоками с `style="height:
#      {{ pct }}%"` внутри `h-12 flex items-end` (на живом скрине бары были
#      полностью невидимы — процент под ними печатался, а сам бар нет).
#      Теперь это тоже готовая SVG-геометрия (`_build_expectation_bars`) —
#      `<rect>` с координатами, посчитанными на сервере, тот же принцип
#      "без JS и без хрупких CSS-трюков с процентной высотой во flex-
#      контейнере", что и у линейных графиков (ADR-0025).
# НЕ трогаем build_sparkline_points выше — её использует partners/
# services.py::build_mood_index_feed (B2B JSON-фид, сырые координаты, не
# готовый SVG, полировка вида туда не относится).
MOOD_CHART_WIDTH = 640
# Mood — главная метрика, крупный график. Trust — вторичная, заметно ниже и
# визуально "тише", чтобы две метрики не соревновались за одно и то же
# внимание (это и убирает "сливаются" на уровне композиции, не только цвета).
MOOD_MAIN_HEIGHT = 148
MOOD_TRUST_HEIGHT = 64
# Просторный левый паддинг — см. пункт 3 докстринга выше: подпись "10" при
# text-anchor="end" должна на 100% помещаться до начала графика.
MOOD_CHART_PAD_X = 32
MOOD_CHART_PAD_TOP = 16
MOOD_CHART_PAD_BOTTOM = 16
MOOD_CHART_VALUE_MAX = 10.0
# Только целые "круглые" значения — см. пункт 2 докстринга выше.
MOOD_CHART_GRID_VALUES = (0, 5, 10)
MOOD_TRUST_GRID_VALUES = (0, 10)  # мини-график — минимум визуального шума

EXPECTATION_BAR_WIDTH = 30
EXPECTATION_BAR_GAP = 16
EXPECTATION_BAR_MAX_H = 40
# БАГ, КОТОРЫЙ ТУТ БЫЛ (живой скрин 2026-09-07: "а хуле тут даты не видно?"):
# высота viewBox была 66, а подпись даты рисуется на `baseline_y + dy` =
# 56 + 14 = 70 — ЗА пределами viewBox. SVG по умолчанию обрезает всё, что
# выходит за границы viewBox (в отличие от обычного HTML, тут нет скролла
# или переноса — контент просто невидим). Дата не "плохо видна", она
# физически не рендерится ни в одном браузере. 84 — с запасом: baseline_y(56)
# + dy(14) + высота строки подписи (~9px шрифта) + пара px на всякий случай.
EXPECTATION_CHART_HEIGHT = 84
EXPECTATION_BASELINE_Y = 56  # оставляет ~28px снизу под подпись даты, ~16px сверху под подпись %

# Порог показа секции "Ожидания перед матчем".
#
# ИСТОРИЯ (2026-09-07, два живых скрина подряд): сначала секция показывалась
# при ЛЮБОМ ненулевом количестве точек — на команде с 1 закрашенным баром из
# 10 (остальные 9 — пунктирные заглушки) это визуально читалось как "почти
# всё сломано", особенно пока бар был не отличим по цвету от графика
# "Доверие" (см. докстринг про --color-accent/--color-secondary чуть выше).
# Подняли порог до "минимум 3 точки И 30% покрытия" — но продуктовый фидбек
# был обратный: "а где эти проценты?" — пользователь хочет видеть данные,
# которые реально есть, даже если их мало, не хочет, чтобы весь блок
# пропадал. Вернули порог к "показываем, если есть хоть одна точка" — теперь,
# когда цвет бара (--color-secondary, розовый) гарантированно отличается от
# --color-success (зелёный/teal), одинокий бар среди приглушённых прочерков
# уже не выглядит поломкой, а прочерки читаются как "нет прогноза на этот
# матч", а не как визуальный мусор.
EXPECTATION_MIN_COVERAGE_COUNT = 1
EXPECTATION_MIN_COVERAGE_RATIO = 0.0


def _build_series_chart(
    series: list[dict], key: str, *,
    width: int, height: int, pad_x: int, pad_top: int, pad_bottom: int,
    value_max: float = MOOD_CHART_VALUE_MAX, grid_values: tuple = MOOD_CHART_GRID_VALUES,
) -> dict:
    """Геометрия ОДНОГО ряда как САМОСТОЯТЕЛЬНОГО мини-графика (своя сетка,
    свой baseline, свой viewBox) — в отличие от прошлой версии, mood и trust
    здесь никогда не делят одну plot-область (см. докстринг модуля выше,
    пункт 1 — это и была причина "графики сливаются").

    `has_data=False` (меньше 2 валидных точек) — шаблон не рисует линию, но
    `latest`/`latest_point` всё равно посчитаны отдельным проходом (последняя
    точка, где `key` не `None`), НЕЗАВИСИМО от `has_data`: если у команды
    всего 1 оценённый матч — крупное число "сейчас" всё равно можно
    показать, даже когда рисовать линию ещё не из чего. Раньше `latest_*`
    брался как `series[-1][key]` без проверки на `None` — если у САМОГО
    свежего матча этой метрики не было (а у более раннего была), получалось
    пустое число при формально непустом блоке (ровно баг со скриншота).
    """
    plot_h = height - pad_top - pad_bottom
    n = len(series)
    step = (width - 2 * pad_x) / max(n - 1, 1)
    baseline_y = pad_top + plot_h

    def scale_y(value: float) -> float:
        clamped = max(0.0, min(value_max, value))
        return pad_top + plot_h - (clamped / value_max) * plot_h

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
        {"x": round(x, 1), "y": round(y, 1), "value": point[key], "label": point["label"], "opponent": point["opponent"]}
        for x, y, point in pts
    ]
    result["polyline"] = polyline
    result["area_path"] = " ".join(area_cmds)
    result["dots"] = dots
    return result


def _build_expectation_bars(series: list[dict]) -> dict:
    """SVG-геометрия ряда "Ожидания перед матчем" — см. пункт 4 докстринга
    модуля: раньше это были HTML/flex `div`-бары с процентной высотой
    (`style="height: {{ pct }}%"`), на живом скрине оказавшиеся полностью
    невидимыми. Здесь координаты каждого `<rect>` посчитаны на сервере явно,
    так же как у линейных графиков — рендерится ОДИНАКОВО предсказуемо в
    любом браузере, а не зависит от тонкостей flex-процентной высоты.

    Матчи без единого прогноза (`pct is None`) — НЕ нулевой бар (0% и
    "прогнозов не было" — разные состояния, см. докстринг compute_mood_series
    у expectation_pct), а отдельный маркер-тире на базовой линии.
    """
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
                "width": EXPECTATION_BAR_WIDTH,  # шаблону нужен даже без бара — для конца пунктирной чёрточки
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


def build_mood_chart(series: list[dict]) -> dict | None:
    """Готовые данные для премиальной отрисовки "Индекса настроения клуба" —
    два независимых мини-графика (mood крупный, trust компактный, см.
    докстринг модуля выше) плюс SVG-бары "ожиданий перед матчем".

    :return: `None`, если `series` пустой (совсем нет оценённых матчей).
    """
    if not series:
        return None

    mood = _build_series_chart(
        series, "mood",
        width=MOOD_CHART_WIDTH, height=MOOD_MAIN_HEIGHT,
        pad_x=MOOD_CHART_PAD_X, pad_top=MOOD_CHART_PAD_TOP, pad_bottom=MOOD_CHART_PAD_BOTTOM,
        grid_values=MOOD_CHART_GRID_VALUES,
    )
    trust = _build_series_chart(
        series, "trust",
        width=MOOD_CHART_WIDTH, height=MOOD_TRUST_HEIGHT,
        pad_x=MOOD_CHART_PAD_X, pad_top=10, pad_bottom=12,
        grid_values=MOOD_TRUST_GRID_VALUES,
    )
    expectation = _build_expectation_bars(series)
    expectation_covered = sum(1 for point in series if point["expectation_pct"] is not None)
    has_expectation_data = (
        expectation_covered >= EXPECTATION_MIN_COVERAGE_COUNT
        and expectation_covered / len(series) >= EXPECTATION_MIN_COVERAGE_RATIO
    )

    return {
        "mood": mood,
        "trust": trust,
        "expectation": expectation,
        "has_expectation_data": has_expectation_data,
        "latest_mood": mood["latest"],
        "latest_trust": trust["latest"],
        "match_count": len(series),
        "oldest_label": f"{series[0]['opponent']} {series[0]['label']}",
        "newest_label": f"{series[-1]['opponent']} {series[-1]['label']}",
    }


def find_season_controversial_matches(team, season, *, limit: int = 3) -> list[dict]:
    """"Самые спорные решения сезона" — сезонная агрегация уже существующего
    матчевого сигнала (RefereeMatchAggregate.home_fans_avg/away_fans_avg,
    см. matches/services.py::_describe_referee_divergence — тот же принцип,
    здесь применён на уровне сезона одной команды, а не одного матча).

    ЯВНОЕ ОГРАНИЧЕНИЕ ЭВРИСТИКИ: это разброс МНЕНИЙ болельщиков о судействе
    (домашние vs гостевые), а не проверенный факт "это было неправильное
    решение" — сайт не может и не должен утверждать судейскую ошибку,
    только показывать, что аудитория по этому матчу разошлась во мнениях
    сильнее обычного. Формулировка в шаблоне должна это отражать (см.
    templates/teams/detail.html).

    Список маленький (сезон одной команды — обычно 20-30 матчей), гэп
    считается в Python, а не SQL ABS() — не стоит городить Func()-выражение
    ради одной сезонной секции, вызываемой раз на рендер страницы команды
    и кэшируемой самим Django cache framework не здесь (см. TeamDetailView —
    страница команды и так не самый горячий путь сайта).
    """
    aggs = list(
        RefereeMatchAggregate.objects.filter(match__season=season)
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
