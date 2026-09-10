# matches/services.py
"""
"ДНК матча" — фаза 1 (docs/PRODUCT_SCOPE_MATCH_DNA_AND_EXPLAINABILITY.md,
раздел 2). Не новые данные — новая композиция уже посчитанного: drama_index
(MatchAggregate, уже существовал), momentum-точки (группировка MatchEvent
по 15-минутным окнам — та же идея, что group-by в
evaluations/views.py::_compute_key_player_ids, только по времени, а не по
типу), и расхождение мнений болельщиков о судействе (RefereeMatchAggregate.
home_fans_avg/away_fans_avg — уже посчитанный сигнал, ранее нигде не
показывался пользователю как отдельная история).

Фаза 1 сознательно текстовая, без шаринговой карточки — см. "Риск" в
scope-документе: цена ошибки на карточке (дизайн/шрифты/легибилити) выше,
чем на текстовой секции, поэтому карточка (фаза 2) — только после проверки,
что эта секция вообще востребована.
"""
from __future__ import annotations

import statistics
from collections import defaultdict

from django.db.models import Count, Q

from matches.models import MatchReaction

MOMENTUM_WINDOW_MINUTES = 15
# Меньше двух событий в окне — не "момент", просто одно событие тайм-лайна,
# отдельно уже видное в блоке "События матча".
MOMENTUM_MIN_EVENTS = 2
# Разница средних оценок судейства двумя лагерями меньше этого — в пределах
# обычного шума мнений, не стоит подавать как "разошлись во мнениях".
REFEREE_DIVERGENCE_MIN_GAP = 1.5

DRAMA_HIGH_THRESHOLD = 60.0   # напр. avg_entertainment=8 * avg_tension=7.5
DRAMA_MEDIUM_THRESHOLD = 30.0  # напр. 6 * 5

# "ДНК матча" фаза 2 (docs/adr/0033-match-dna-phase2.md, раздел
# Match DNA) — герой матча/переломный момент/консенсус/спорный эпизод.
# Порог "достаточно голосов, чтобы называть кого-то героем" — тот же
# MIN_VOTES_FOR_DISPLAY, что уже применяется к top_players на странице
# матча (matches/views.py), передаём готовый отфильтрованный список, а не
# фильтруем повторно здесь.
TURNING_POINT_MIN_RATIO = 0.3
# Разброс (population stdev) композитной оценки матча voter'ом
# ((entertainment+tension+fairness)/3) по шкале 1-10. <= HIGH — мнения
# почти совпадают, >= LOW — мнения разошлись сильно, между — средний
# консенсус. Подобрано по тому же принципу, что и пороги драмы выше —
# ориентир на разницу в 1-2.5 балла по 10-балльной шкале, а не строгая
# статистическая калибровка (данных для неё пока недостаточно).
CONSENSUS_HIGH_STDEV = 1.0
CONSENSUS_LOW_STDEV = 2.5

# "ДНК матча" фаза 3 (docs/adr/0034-match-dna-phase3-antihero-fan-mood.md) —
# антигерой матча и настроение фанатов. Третий аудит (Codex, 2026-09-07)
# отдельно назвал их недостающими в шаринговой карточке. Порог "достаточно
# голосов за поддерживаемую команду, чтобы подавать процент как факт" —
# тот же принцип, что и у остальных гейтов этого модуля (REFEREE_DIVERGENCE_MIN_GAP,
# TURNING_POINT_MIN_RATIO): маленькая выборка не должна выглядеть как
# уверенное утверждение.
FAN_MOOD_MIN_VOTES = 3


def _drama_level(drama_index: float) -> str:
    if drama_index >= DRAMA_HIGH_THRESHOLD:
        return "high"
    if drama_index >= DRAMA_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def _pluralize_goals(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "гол"
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return "гола"
    return "голов"


def _describe_momentum(events) -> list[str]:
    """events — итерируемый список MatchEvent (не queryset, чтобы не бить
    по БД повторно, если вызывающая сторона уже материализовала список для
    _match_events.html). Возвращает 0-2 строки, отсортированные по числу
    событий в 15-минутном окне (см. MOMENTUM_WINDOW_MINUTES)."""
    buckets: dict[int, list] = defaultdict(list)
    for event in events:
        window_start = (event.minute // MOMENTUM_WINDOW_MINUTES) * MOMENTUM_WINDOW_MINUTES
        buckets[window_start].append(event)

    scored = sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True)
    points = []
    for window_start, bucket_events in scored[:2]:
        if len(bucket_events) < MOMENTUM_MIN_EVENTS:
            break
        window_end = window_start + MOMENTUM_WINDOW_MINUTES
        goals = sum(1 for e in bucket_events if e.event_type == "goal")
        if goals >= 2:
            points.append(f"{goals} {_pluralize_goals(goals)} в отрезке {window_start}–{window_end}'")
        else:
            points.append(f"{len(bucket_events)} событий в отрезке {window_start}–{window_end}'")
    return points


def _describe_referee_divergence(match, referee_agg) -> str:
    """RefereeMatchAggregate.home_fans_avg/away_fans_avg — уже посчитанный
    сигнал (aggregates/tasks.py::recalculate_referee_aggregates), просто
    никогда не выводился пользователю как отдельная история. Гейт "меньше
    2 сегментов — сравнивать не с чем" — тот же принцип, что у
    core/templatetags/rating_extras.py::bias_segment_text."""
    if referee_agg is None:
        return ""
    home_avg = referee_agg.home_fans_avg
    away_avg = referee_agg.away_fans_avg
    if home_avg is None or away_avg is None:
        return ""
    gap = abs(home_avg - away_avg)
    if gap < REFEREE_DIVERGENCE_MIN_GAP:
        return ""
    return (
        f"Болельщики {match.home_team.name} и {match.away_team.name} разошлись "
        f"во мнениях о судействе на {gap:.1f} балла."
    )


def _describe_hero(top_players: list) -> dict | None:
    """"Герой матча" — просто первая строка уже отфильтрованного и
    отсортированного `top_players` (matches/views.py: PlayerMatchAggregate
    с total_votes >= MIN_VOTES_FOR_DISPLAY, order_by('-performance_score')) —
    никакого нового запроса, тот же список, что уже рендерится в "Топ
    игроков матча" чуть ниже на странице."""
    if not top_players:
        return None
    hero_agg = top_players[0]
    return {"player": hero_agg.player, "score": hero_agg.performance_score}


def _describe_antihero(top_players: list, worst_players: list) -> dict | None:
    """"Антигерой матча" — первая строка уже отфильтрованного и
    отсортированного ПО ВОЗРАСТАНИЮ `worst_players` (тот же порог
    total_votes >= MIN_VOTES_FOR_DISPLAY, что и у top_players/_describe_hero —
    `matches/views.py::MatchDetailView` уже считает этот список для
    антифрод/качественной витрины, но до фазы 3 нигде его не показывал:
    данные были, витрины не было — та же формулировка, что и у
    _describe_turning_point до фазы 2).

    Не показываем антигероя, если он оказался ТЕМ ЖЕ игроком, что и герой —
    это происходит, когда после фильтра по MIN_VOTES_FOR_DISPLAY остался
    только один игрок: "антигерой" и "герой" совпадали бы, что вводит в
    заблуждение (не "второй полюс", а тот же самый человек).
    """
    if not worst_players:
        return None
    antihero_agg = worst_players[0]
    hero_agg = top_players[0] if top_players else None
    if hero_agg is not None and antihero_agg.player_id == hero_agg.player_id:
        return None
    return {"player": antihero_agg.player, "score": antihero_agg.performance_score}


def _describe_fan_mood(match_aggregate, fan_support: list) -> str:
    """"Настроение фанатов" — зрелищность матча (MatchAggregate.avg_entertainment,
    уже посчитана) плюс перекос трибун (ContextEvaluation.supported_team) —
    тот же агрегат, что matches/views.py уже считает для блока "За кого
    болели" (fan_support: до 2 строк вида {'supported_team__name', 'count'},
    отсортированных по count по убыванию), просто раньше не соединялся с
    зрелищностью в одну "настроенческую" фразу нигде на странице.

    :param fan_support: список dict'ов (не queryset — вызывающая сторона
        уже материализовала его для шаблона, здесь только читаем).
    :return: пустая строка, если голосов за поддерживаемую команду меньше
        FAN_MOOD_MIN_VOTES (шум) — то же решение "не гадать", что у
        _describe_referee_divergence/_describe_turning_point.
    """
    if not fan_support:
        return ""
    total = sum(row["count"] for row in fan_support)
    if total < FAN_MOOD_MIN_VOTES:
        return ""
    dominant = fan_support[0]
    pct = round(dominant["count"] / total * 100)
    return (
        f"Зрелищность матча болельщики оценили на {match_aggregate.avg_entertainment:.1f}/10, "
        f"{pct}% из проголосовавших за команду поддерживали {dominant['supported_team__name']}."
    )


def _describe_consensus_text(consensus_level: str | None) -> str:
    """Текстовая версия `_consensus_level` (та возвращает только 'high'/
    'medium'/'low'/None — для бейджа на странице этого достаточно, но
    шаринговая карточка (Pillow, `core/services/share_cards.py`) рисует
    обычный текст, не бейджи с цветом)."""
    if consensus_level == "high":
        return "Болельщики почти единодушны в оценке этого матча."
    if consensus_level == "low":
        return "Мнения о матче разошлись сильно — единого впечатления нет."
    if consensus_level == "medium":
        return "Мнения о матче разошлись умеренно."
    return ""


def _describe_turning_point(match_aggregate) -> str:
    """turning_point_ratio — уже посчитанное поле MatchAggregate (доля
    голосовавших, отметивших чекбокс "был переломный момент?" на
    match_final.html), посчитанное, но нигде не показанное пользователю до
    этой правки (см. docs/CODEX_AUDIT_RESPONSE_2026-09-07.md — данные есть,
    витрины не было)."""
    ratio = match_aggregate.turning_point_ratio
    if ratio < TURNING_POINT_MIN_RATIO:
        return ""
    pct = round(ratio * 100)
    return f"{pct}% зрителей отметили явный переломный момент в этом матче."


def _consensus_level(match_evaluations: list) -> str | None:
    """"Уровень консенсуса" — population stdev композитной оценки
    ((entertainment+tension+fairness)/3) КАЖДОГО голосовавшего, а не stdev
    уже усреднённых чисел (это было бы стандартной ошибкой средних, не
    разбросом мнений). :param match_evaluations: сырые MatchEvaluation этого
    матча (matches/views.py передаёт `.only(...)` по трём полям — читать
    их всё равно нужно построчно, агрегат MatchAggregate такого разброса не
    хранит). :return: None при < 2 голосах (сравнивать не с чем)."""
    composites = [(e.entertainment + e.tension + e.fairness) / 3 for e in match_evaluations]
    if len(composites) < 2:
        return None
    stdev = statistics.pstdev(composites)
    if stdev <= CONSENSUS_HIGH_STDEV:
        return "high"
    if stdev >= CONSENSUS_LOW_STDEV:
        return "low"
    return "medium"


def _describe_controversial_episode(events: list, referee_aggregate) -> str:
    """"Главный спорный эпизод" — эвристика на основе уже существующих
    сигналов, НЕ отдельное голосование за "самый спорный момент" (такого
    механизма в продукте нет, честно отмечаем это ограничение здесь, а не
    выдаём эвристику за точный факт):

    1. Отменённый гол (event_type='disallowed_goal') — самый однозначный
       прокси "спорности" из доступных: решение под вопросом ПО
       ОПРЕДЕЛЕНИЮ (VAR/офсайд/повтор), не требует доп. сигналов.
    2. Иначе, только если фанаты разошлись во мнениях о судействе
       (см. _describe_referee_divergence — тот же порог REFEREE_DIVERGENCE_MIN_GAP,
       переиспользуем как признак "было о чём спорить") — берём первую
       красную карточку матча как вероятную причину этого расхождения.
       Без гейта на расхождение красная карточка сама по себе слишком
       обычное событие футбола, чтобы называть её "спорной".
    3. Если ни то, ни другое — возвращаем "", не гадаем.
    """
    disallowed = [e for e in events if e.event_type == "disallowed_goal"]
    if disallowed:
        event = disallowed[0]
        who = f" ({event.player})" if event.player_id else ""
        return f"Отменённый гол на {event.display_minute}'{who} — главный спорный эпизод матча."

    if referee_aggregate is not None:
        home_avg, away_avg = referee_aggregate.home_fans_avg, referee_aggregate.away_fans_avg
        if home_avg is not None and away_avg is not None and abs(home_avg - away_avg) >= REFEREE_DIVERGENCE_MIN_GAP:
            red_cards = [e for e in events if e.event_type == "red_card"]
            if red_cards:
                event = red_cards[0]
                who = f" ({event.player})" if event.player_id else ""
                gap = abs(home_avg - away_avg)
                return (
                    f"Красная карточка на {event.display_minute}'{who} — вероятно, "
                    f"самый спорный момент матча (мнения о судействе разошлись на {gap:.1f} балла)."
                )
    return ""


def build_match_dna(
    match, match_aggregate, events, referee_aggregate=None,
    match_evaluations: list | None = None, top_players: list | None = None,
    worst_players: list | None = None, fan_support: list | None = None,
) -> dict | None:
    """Собирает контекст для секции "ДНК матча" на странице матча.

    :param match_aggregate: уже полученный `match.aggregate` (MatchAggregate
        либо None) — не делаем повторный запрос, вызывающая сторона
        (matches/views.py::MatchDetailView) его и так уже получает.
    :param events: список/queryset MatchEvent этого матча (для momentum и,
        с фазы 2, controversial_episode).
    :param referee_aggregate: `match.referee_aggregates.first()` либо None.
    :param match_evaluations: сырые MatchEvaluation этого матча (фаза 2,
        для consensus_level) — `None`/пустой список даёт consensus_level=None,
        секция при этом не ломается, просто не показывает эту строку.
    :param top_players: уже отфильтрованный/отсортированный список
        PlayerMatchAggregate (фаза 2, для hero) — тот же список, что
        matches/views.py передаёт в шаблон как `top_players`.
    :param worst_players: список PlayerMatchAggregate, отсортированный ПО
        ВОЗРАСТАНИЮ performance_score, тот же порог total_votes, что и
        top_players (фаза 3, для antihero) — matches/views.py уже считает
        его как `worst_players`, просто раньше нигде не показывал.
    :param fan_support: до 2 dict'ов {'supported_team__name', 'count'} —
        тот же агрегат, что matches/views.py считает для блока "За кого
        болели" (фаза 3, для fan_mood_text).
    :return: None, если голосов по матчу ещё нет вообще (нечего показывать —
        секция не должна рендериться пустой рамкой), иначе dict с ключами
        drama_index, drama_level ('high'/'medium'/'low'), momentum_points
        (list[str], может быть пустым), referee_divergence (str, может быть
        пустой), hero (dict{'player','score'} | None), antihero
        (dict{'player','score'} | None), turning_point_text (str, может быть
        пустой), consensus_level ('high'/'medium'/'low'/None), consensus_text
        (str, может быть пустой — текстовая версия consensus_level),
        fan_mood_text (str, может быть пустой), controversial_episode (str,
        может быть пустой).
    """
    if match_aggregate is None or match_aggregate.total_votes == 0:
        return None

    top_players = top_players or []
    consensus_level = _consensus_level(match_evaluations or [])

    return {
        "drama_index": match_aggregate.drama_index,
        "drama_level": _drama_level(match_aggregate.drama_index),
        "momentum_points": _describe_momentum(events),
        "referee_divergence": _describe_referee_divergence(match, referee_aggregate),
        "hero": _describe_hero(top_players),
        "antihero": _describe_antihero(top_players, worst_players or []),
        "turning_point_text": _describe_turning_point(match_aggregate),
        "consensus_level": consensus_level,
        "consensus_text": _describe_consensus_text(consensus_level),
        "fan_mood_text": _describe_fan_mood(match_aggregate, fan_support or []),
        "controversial_episode": _describe_controversial_episode(events, referee_aggregate),
    }


# ---------------------------------------------------------------------------
# РЕДИЗАЙН КАРТОЧКИ МАТЧА (2026-09-10, прямая просьба пользователя, полный
# бриф из 14 пунктов — "интрига, прогноз сообщества, игрок матча DOPX,
# Match DNA, индекс сенсации и т.д."). Функции ниже — чистая композиция уже
# существующих сигналов в короткие фразы/бейджи ДЛЯ КАРТОЧКИ СПИСКА
# (upcoming/finished), сознательно ОТДЕЛЬНО от build_match_dna() выше:
# та функция — полная секция страницы одного матча (требует events,
# match_evaluations и т.д. — дорого на карточку в списке из 20 штук),
# здесь — минимальный бесплатный набор, безопасный для рендера пачками
# (см. matches/card_services.py — bulk-оркестрация для списка матчей).
# ---------------------------------------------------------------------------

# Минимум голосов, чтобы карточка вообще показывала мини-ДНК — тот же
# принцип "маленькая выборка не должна выглядеть уверенным утверждением",
# что и FAN_MOOD_MIN_VOTES выше, только применённый к total_votes матча
# целиком, а не к одной подгруппе.
CARD_DNA_MIN_VOTES = 3

# Минимум прогнозов, чтобы индекс сенсации вообще что-то значил — один
# голос "против" не делает результат сенсацией, просто у него мало данных.
SENSATION_MIN_PREDICTIONS = 5

# С какой минуты гол считается "поздним/решающим" для карточки "главный
# момент" — тот же порядок величины, что MOMENTUM_WINDOW_MINUTES*5 (75-90'
# уже используется как пример "концовка" в других местах проекта, см.
# докстринг _describe_momentum выше).
LATE_GOAL_MINUTE_THRESHOLD = 75

# "Битва за топ-N" / "матч за выживание" — пороги позиций в таблице.
INTRIGUE_TOP_BATTLE_POSITION = 3
INTRIGUE_RELEGATION_ZONE_SIZE = 3
# Разница мячей в предыдущей очной встрече, начиная с которой имеет смысл
# подавать следующую игру как "реванш" — 1-2 гола это обычный футбольный
# результат, не повод для отдельного нарратива.
INTRIGUE_REVENGE_MARGIN = 3


def describe_intrigue(match, *, home_position=None, away_position=None, total_teams=None, last_meeting=None) -> str | None:
    """Пункт 1 брифа — короткий тег интриги под составом. Приоритет (первое
    подходящее побеждает — карточка показывает ОДИН тег, не список):

    1. Дерби (`match.is_derby` — уже существующий сигнал, админский список
       `Team.rivals`, полностью переиспользуется, не дублируется).
    2. Битва за топ-N — обе команды сейчас входят в верхние
       INTRIGUE_TOP_BATTLE_POSITION мест таблицы.
    3. Матч за выживание — обе команды в нижних INTRIGUE_RELEGATION_ZONE_SIZE
       местах (зона вычисляется от `total_teams`, а не захардкожена — число
       команд в лиге не константа проекта).
    4. Реванш — последняя очная встреча закончилась разгромом (>= INTRIGUE_
       REVENGE_MARGIN мячей) в пользу ТЕКУЩЕГО соперника проигравшей тогда
       команды: следующая игра между теми же командами подписывается как
       "Реванш за X:Y".

    :param home_position, away_position: текущая позиция в таблице
        (TeamSeasonStats.position) — None, если ещё не посчитана.
    :param last_meeting: dict {'home_team_id', 'away_team_id', 'home_score',
        'away_score'} последней очной встречи ДО этого матча (любой из двух
        мог тогда играть дома) либо None, если очных встреч не было.
    :return: готовая строка тега либо None — карточка просто не показывает
        блок интриги, не показывает пустой тег.
    """
    if getattr(match, 'is_derby', False):
        return 'Дерби'

    if (
        home_position is not None and away_position is not None
        and home_position <= INTRIGUE_TOP_BATTLE_POSITION and away_position <= INTRIGUE_TOP_BATTLE_POSITION
    ):
        return f'Битва за топ-{INTRIGUE_TOP_BATTLE_POSITION}'

    if home_position is not None and away_position is not None and total_teams:
        relegation_from = total_teams - INTRIGUE_RELEGATION_ZONE_SIZE + 1
        if home_position >= relegation_from and away_position >= relegation_from:
            return 'Матч за выживание'

    if last_meeting:
        home_score = last_meeting.get('home_score')
        away_score = last_meeting.get('away_score')
        if home_score is not None and away_score is not None and home_score != away_score:
            margin = abs(home_score - away_score)
            if margin >= INTRIGUE_REVENGE_MARGIN:
                # Кто тогда проиграл разгромно — это и есть "жаждущая
                # реванша" сторона; текущий матч ей интересен именно так,
                # независимо от того, дома она сейчас играет или в гостях.
                loser_score, winner_score = min(home_score, away_score), max(home_score, away_score)
                return f'Реванш за {loser_score}:{winner_score}'

    return None


def describe_key_moment(match, events: list) -> str | None:
    """Пункт 8 брифа — "главный момент" завершённого матча одной строкой.
    Эвристика (та же дисциплина, что у `_describe_controversial_episode`
    выше — явный приоритет, "" вместо гадания, если ничего не подходит):

    1. Поздний гол (>= LATE_GOAL_MINUTE_THRESHOLD'), который менял разницу
       мячей до 1 или срав­нивал счёт — по построению это последний гол
       матча (события отсортированы по минуте, см. `MatchEvent.Meta.ordering`)
       на такой минуте почти всегда и есть "решивший исход".
    2. Красная карточка — карточка меняет ход игры сама по себе, даже без
       дальнейшего гола.
    3. Пенальти (реализованный) — редкое, заметное событие.
    4. Иначе — None, не сочиняем историю на пустом месте (например, сухая
       ничья без единого примечательного события).

    :param events: список MatchEvent (не queryset), отсортированный по
        минуте — обычной страницы события уже приходят так (Meta.ordering).
    """
    if match.decided_administratively or not events:
        return None

    goals = [e for e in events if e.event_type in ('goal', 'penalty', 'own_goal')]
    if goals:
        last_goal = goals[-1]
        if last_goal.minute >= LATE_GOAL_MINUTE_THRESHOLD:
            who = f' ({last_goal.player})' if last_goal.player_id else ''
            return f'Гол на {last_goal.display_minute}\'{who} решил исход матча'

    red_cards = [e for e in events if e.event_type == 'red_card']
    if red_cards:
        event = red_cards[0]
        who = f' ({event.player})' if event.player_id else ''
        return f'Красная карточка на {event.display_minute}\'{who}'

    penalties = [e for e in events if e.event_type == 'penalty']
    if penalties:
        event = penalties[0]
        who = f' ({event.player})' if event.player_id else ''
        return f'Пенальти на {event.display_minute}\'{who}'

    return None


def describe_card_dna_traits(aggregate) -> list[str]:
    """Пункт 10 брифа — мини-версия "ДНК матча" ПРЯМО НА КАРТОЧКЕ (не
    путать с полным виджетом `_match_dna_card.html`/`build_match_dna()`
    выше — это два разных места, см. их докстринги). Специально считается
    ТОЛЬКО из уже загруженных полей `MatchAggregate` (drama_index,
    avg_tension, avg_fairness, turning_point_ratio) — БЕЗ единого
    дополнительного запроса на карточку (ни events, ни MatchEvaluation):
    на странице списка из 10-20 карточек лишний запрос на каждую было бы
    ровно тем N+1, из-за которого в проекте уже есть `bulk_prediction_data`
    и подобные bulk-функции.

    :return: 0-3 коротких строки-трейта (может быть пустым — карточка
        просто не показывает блок).
    """
    if aggregate is None or aggregate.total_votes < CARD_DNA_MIN_VOTES:
        return []

    traits = []
    level = _drama_level(aggregate.drama_index)
    if level == 'high':
        traits.append('Высокая драма')
    elif level == 'medium':
        traits.append('Умеренная интрига')

    # 0 < avg_fairness — при total_votes >= CARD_DNA_MIN_VOTES это всегда
    # настоящее среднее по шкале 1-10 (см. _consensus_level выше), не
    # дефолтное 0.0 "голосов нет" — та ветка уже отсечена гейтом выше,
    # поэтому здесь достаточно сравнения без доп. truthy-проверки.
    if 0 < aggregate.avg_fairness <= 5.0:
        traits.append('Жёсткая игра')

    if aggregate.turning_point_ratio >= TURNING_POINT_MIN_RATIO:
        traits.append('Был перелом')

    return traits[:3]


def compute_sensation_index(match, counts: dict | None) -> int | None:
    """Пункт 12 брифа — "Индекс сенсации", 0-100, показывается ТОЛЬКО когда
    итог разошёлся с ожиданиями сообщества (см. return None ниже). Формула
    — эвристика, не строгая статистика (тот же честный принцип, что у
    `_describe_referee_divergence`/`find_season_controversial_matches`:
    открыто фиксируем ограничение прямо в докстринге, а не выдаём число за
    точную науку): "насколько уверенно сообщество ошиблось" — доля голосов
    за фаворита (по прогнозам ДО матча), который в итоге НЕ выиграл.
    Чем увереннее (выше %) было ошибочное большинство — тем выше сенсация.

    :param counts: dict от `predictions.services.prediction_counts()`/
        `bulk_final_prediction_counts()` (home/draw/away/total/*_pct) —
        распределение прогнозов ДО матча, а не гейтовано `is_prediction_open`
        (окно давно закрыто у завершённого матча, но строки прогнозов
        остаются — см. докстринг `bulk_final_prediction_counts`).
    :return: None, если прогнозов меньше SENSATION_MIN_PREDICTIONS (мало
        данных — не сенсация, просто нечем измерить), либо если фаворит
        сообщества и совпал с реальным исходом (предсказуемый результат —
        по определению не сенсация, бейдж вообще не должен показываться).
    """
    if not counts or counts.get('total', 0) < SENSATION_MIN_PREDICTIONS:
        return None
    final_result = match.final_result
    if final_result is None:
        return None

    pct_by_choice = {'1': counts['home_pct'], 'X': counts['draw_pct'], '2': counts['away_pct']}
    favorite_choice = max(pct_by_choice, key=pct_by_choice.get)
    if favorite_choice == final_result:
        return None  # сообщество угадало фаворита — предсказуемый результат

    return round(pct_by_choice[favorite_choice])


def describe_table_impact(team, before_position: int | None, current_position: int | None) -> str | None:
    """Пункт 13 брифа — "Изменил таблицу" (чистая функция, без запросов —
    позиции считает вызывающая сторона, см. matches/card_services.py и
    teams/services.py::compute_standings_asof)."""
    if before_position is None or current_position is None or before_position == current_position:
        return None
    if current_position < before_position:
        return f'{team.name} поднялся на {current_position}-е место'
    return f'{team.name} опустился на {current_position}-е место'


def describe_finished_cta(has_hero: bool, has_dna: bool) -> dict:
    """Пункт 14 брифа — замена бейджа "Голосование закрыто" на CTA со
    смыслом. Карточка и так целиком <a> на страницу матча (см.
    components/_match_card.html) — это НЕ отдельная ссылка, а более
    информативный ярлык того же места, куда клик уже ведёт."""
    if has_hero or has_dna:
        return {'icon': 'ti-chart-bar', 'label': 'Разобрать матч'}
    return {'icon': 'ti-star', 'label': 'Смотреть оценки игроков'}


# --- Реакция сообщества на завершённый матч (пункт 11 брифа) — тот же
# сервисный паттерн, что predictions/services.py::submit_prediction/
# prediction_counts/bulk_prediction_data, только на модели MatchReaction. ---

def submit_match_reaction(*, user, match, reaction: str):
    """Ставит/меняет реакцию пользователя на завершённый матч. Доступно
    только для `status='finished'` — реагировать "скучный матч"/"матч тура"
    до финального свистка не имеет смысла (в отличие от прогноза, у
    реакции нет собственного окна времени — единственное условие это сам
    факт, что матч уже сыгран)."""
    if match.status != 'finished':
        return None
    reaction_codes = dict(MatchReaction.REACTION_CHOICES)
    if reaction not in reaction_codes:
        return None
    obj, _created = MatchReaction.objects.update_or_create(
        match=match, user=user, defaults={'reaction': reaction},
    )
    return obj


def reaction_counts(match) -> dict:
    """Один матч — распределение реакций сообщества, тот же принцип
    округления в Python, что `predictions.services.prediction_counts()`."""
    row = MatchReaction.objects.filter(match=match).aggregate(
        match_of_round=Count('id', filter=Q(reaction=MatchReaction.REACTION_MATCH_OF_ROUND)),
        upset=Count('id', filter=Q(reaction=MatchReaction.REACTION_UPSET)),
        boring=Count('id', filter=Q(reaction=MatchReaction.REACTION_BORING)),
    )
    total = row['match_of_round'] + row['upset'] + row['boring']

    def pct(n: int) -> int:
        return round(n * 100 / total) if total else 0

    return {
        'match_of_round': row['match_of_round'], 'upset': row['upset'], 'boring': row['boring'],
        'total': total,
        'match_of_round_pct': pct(row['match_of_round']),
        'upset_pct': pct(row['upset']),
        'boring_pct': pct(row['boring']),
    }


def user_match_reaction(user, match):
    if not user or not user.is_authenticated:
        return None
    return MatchReaction.objects.filter(match=match, user=user).first()


def bulk_reaction_data(matches, user) -> dict:
    """Bulk-версия reaction_counts()/user_match_reaction() выше — тот же
    принцип, что predictions.services.bulk_prediction_data() (см. её
    докстринг про N+1 на карточках списка), но БЕЗ фильтра по открытому
    окну (у реакции нет окна — единственное условие уже применено
    вызывающей стороной, matches/card_services.py, которая передаёт сюда
    только status='finished' матчи)."""
    matches = list(matches)
    if not matches:
        return {}
    match_ids = [m.id for m in matches]

    counts_by_match = {
        m_id: {'match_of_round': 0, 'upset': 0, 'boring': 0, 'total': 0,
               'match_of_round_pct': 0, 'upset_pct': 0, 'boring_pct': 0}
        for m_id in match_ids
    }
    rows = (
        MatchReaction.objects.filter(match_id__in=match_ids)
        .values('match_id', 'reaction').annotate(n=Count('id'))
    )
    for row in rows:
        key = row['reaction']
        if key in counts_by_match[row['match_id']]:
            counts_by_match[row['match_id']][key] = row['n']
    for counts in counts_by_match.values():
        total = counts['match_of_round'] + counts['upset'] + counts['boring']
        counts['total'] = total
        for key in ('match_of_round', 'upset', 'boring'):
            counts[f'{key}_pct'] = round(counts[key] * 100 / total) if total else 0

    my_reactions = {}
    if user and user.is_authenticated:
        my_reactions = {
            r.match_id: r for r in MatchReaction.objects.filter(match_id__in=match_ids, user=user)
        }

    return {
        m_id: {'counts': counts_by_match[m_id], 'my_reaction': my_reactions.get(m_id)}
        for m_id in match_ids
    }
