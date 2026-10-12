# players/formations.py
"""Схема сборной (тура/сезона) подбирается по данным, а не всегда 4-3-3.

Кандидаты — схемы, которыми реально играли команды (MatchLineup.formation). Для каждой
оптимальная расстановка (венгерский алгоритм) по сглаженным рейтингам; побеждает схема
с наибольшей суммой рейтингов 11 игроков плюс небольшой бонус за популярность (POPULARITY_WEIGHT).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from core.utils import ru_num

# Амплуа слота -> (основные коды, близкие, запасные — «если больше некого»). Коды — из positions.resolve_lineup_codes.
# Латерали в схемах с тремя защитниками поставщик даёт как LM/RM.
ROLE_CODES: dict[str, tuple[list[str], list[str], list[str]]] = {
    "GK": (["GK:C", "GK"], [], []),
    "CB": (["D:C", "CB", "DF", "D"], ["DM:C"], []),
    "RB": (["D:R", "RB", "RWB"], ["M:R"], ["D:C"]),
    "LB": (["D:L", "LB", "LWB"], ["M:L"], ["D:C"]),
    "RWB": (["M:R", "RM", "D:R", "RB", "RWB"], ["AM:R"], []),
    "LWB": (["M:L", "LM", "D:L", "LB", "LWB"], ["AM:L"], []),
    "DM": (["DM:C", "DM", "M:C", "CM", "M", "MF"], ["AM:C"], ["D:C"]),
    "CM": (["M:C", "CM", "M", "MF", "DM:C", "DM"], ["AM:C"], ["M:L", "M:R"]),
    "RM": (["M:R", "RM"], ["AM:R", "RW"], ["D:R"]),
    "LM": (["M:L", "LM"], ["AM:L", "LW"], ["D:L"]),
    "AM": (["AM:C", "AM"], ["M:C", "CM"], ["F:C"]),
    "RW": (["AM:R", "RW", "F:R", "W"], ["M:R", "RM"], ["F:C", "AM:C"]),
    "LW": (["AM:L", "LW", "F:L", "W"], ["M:L", "LM"], ["F:C", "AM:C"]),
    "ST": (["F:C", "ST", "CF", "F", "FW"], ["F:L", "F:R"], ["AM:C"]),
}
# Коэффициент к рейтингу по уровню соответствия позиции.
FIT_FACTORS = (1.0, 0.92, 0.75)

ROLE_LABELS: dict[str, str] = {
    "GK": "Вратарь", "CB": "Центральный защитник", "RB": "Правый защитник", "LB": "Левый защитник",
    "RWB": "Правый латераль", "LWB": "Левый латераль", "DM": "Опорный полузащитник",
    "CM": "Центральный полузащитник", "RM": "Правый полузащитник", "LM": "Левый полузащитник",
    "AM": "Атакующий полузащитник", "RW": "Правый вингер", "LW": "Левый вингер", "ST": "Нападающий",
    "COACH": "Главный тренер", "REFEREE": "Судья",
}

_BACK4 = ["LB", "CB", "CB", "RB"]
_BACK3 = ["CB", "CB", "CB"]
_BACK5 = ["LWB", "CB", "CB", "CB", "RWB"]

# Ряды от атаки к своим воротам, слева направо; вратарь добавляется сам.
FORMATIONS: dict[str, list[list[str]]] = {
    "4-3-3": [["LW", "ST", "RW"], ["CM", "DM", "CM"], _BACK4],
    "4-2-3-1": [["ST"], ["LW", "AM", "RW"], ["DM", "DM"], _BACK4],
    "4-4-2": [["ST", "ST"], ["LM", "CM", "CM", "RM"], _BACK4],
    "4-4-1-1": [["ST"], ["AM"], ["LM", "CM", "CM", "RM"], _BACK4],
    "4-1-4-1": [["ST"], ["LM", "CM", "CM", "RM"], ["DM"], _BACK4],
    "4-1-3-2": [["ST", "ST"], ["LM", "CM", "RM"], ["DM"], _BACK4],
    "4-3-1-2": [["ST", "ST"], ["AM"], ["CM", "DM", "CM"], _BACK4],
    "3-4-3": [["LW", "ST", "RW"], ["LWB", "CM", "CM", "RWB"], _BACK3],
    "3-4-2-1": [["ST"], ["AM", "AM"], ["LWB", "CM", "CM", "RWB"], _BACK3],
    "3-4-1-2": [["ST", "ST"], ["AM"], ["LWB", "CM", "CM", "RWB"], _BACK3],
    "3-5-2": [["ST", "ST"], ["LWB", "CM", "DM", "CM", "RWB"], _BACK3],
    "3-1-4-2": [["ST", "ST"], ["LWB", "CM", "CM", "RWB"], ["DM"], _BACK3],
    "3-5-1-1": [["ST"], ["AM"], ["LWB", "CM", "DM", "CM", "RWB"], _BACK3],
    "5-4-1": [["ST"], ["LM", "CM", "CM", "RM"], _BACK5],
    "5-3-2": [["ST", "ST"], ["CM", "DM", "CM"], _BACK5],
}
DEFAULT_FORMATION = "4-3-3"


def formation_rows(name: str) -> list[list[tuple[str, str]]]:
    """Ряды (атака -> вратарь) из пар (код слота, амплуа). Повторяющееся амплуа нумеруется: CB1, CB2…"""
    rows = FORMATIONS.get(name) or FORMATIONS[DEFAULT_FORMATION]
    rows = rows + [["GK"]]
    totals = Counter(role for row in rows for role in row)
    seen: Counter = Counter()
    result = []
    for row in rows:
        out = []
        for role in row:
            seen[role] += 1
            out.append((f"{role}{seen[role]}" if totals[role] > 1 else role, role))
        result.append(out)
    return result


def formation_slots(name: str) -> list[tuple[str, str, int]]:
    """(код слота, амплуа, порядок отображения: от вратаря к атаке)."""
    rows = formation_rows(name)
    ordered = [pair for row in reversed(rows) for pair in row]
    return [(code, role, i) for i, (code, role) in enumerate(ordered, start=1)]


def _row_label(roles: list[str]) -> str:
    kinds = set(roles)
    if kinds == {"GK"}:
        return "Вратарь"
    if kinds <= {"ST", "LW", "RW"}:
        return "Атака"
    if "CB" in kinds:
        return "Защита"
    if kinds == {"DM"}:
        return "Опорная зона"
    if "AM" in kinds and not kinds & {"CM", "DM"}:
        return "Атакующая полузащита"
    return "Полузащита"


def pitch_rows(name: str) -> list[tuple[str, list[str]]]:
    """(подпись ряда, коды слотов) от атаки к вратарю — для поля на страницах и виджетах."""
    return [(_row_label([role for _c, role in row]), [code for code, _r in row]) for row in formation_rows(name)]


def slot_label(slot_code: str) -> str:
    return ROLE_LABELS.get(slot_code.rstrip("0123456789"), slot_code)


def normalize_formation(raw: str | None) -> str | None:
    value = (raw or "").strip().replace(" ", "")
    return value if value in FORMATIONS else None


def formations_used(**match_filter) -> Counter:
    """{схема: сколько раз ей выходили команды} по составам матчей (фильтр — по полям Match)."""
    from lineups.models import MatchLineup

    raw = MatchLineup.objects.filter(**{f"match__{k}": v for k, v in match_filter.items()}).values_list("formation", flat=True)
    used = Counter(normalize_formation(f) for f in raw)
    used.pop(None, None)
    return used


def _hungarian(cost: list[list[float]]) -> list[int]:
    """Минимальная стоимость назначения строк столбцам (строк <= столбцов). Классический O(n²m)."""
    n, m = len(cost), len(cost[0])
    inf = float("inf")
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break
    result = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            result[p[j] - 1] = j - 1
    return result


@dataclass
class FormationChoice:
    name: str
    by_slot: dict  # {код слота: [(кандидат, рейтинг), …]}, занявший слот — первым
    slots: list  # formation_slots(name)
    total: float
    used: int
    alternatives: list = field(default_factory=list)  # [(схема, сумма)] по убыванию
    note: str = ""


def _role_rankings(pool_by_code: dict, rank, key) -> dict:
    """{амплуа: {ключ: (кандидат, рейтинг, коэффициент)}}. Сглаживание — по пулу амплуа, как раньше по пулу слота."""
    out = {}
    for role, tiers in ROLE_CODES.items():
        fit, cands, seen = {}, [], set()
        for codes, factor in zip(tiers, FIT_FACTORS):
            for code in codes:
                for cand in pool_by_code.get(code, []):
                    k = key(cand)
                    if k not in seen:
                        seen.add(k)
                        cands.append(cand)
                        fit[k] = factor
        out[role] = {key(c): (c, score, fit[key(c)]) for c, score in rank(cands)}
    return out


def _fill(name: str, roles: dict, key):
    slots = formation_slots(name)
    candidates = {}
    for _code, role, _order in slots:
        for k, (cand, _s, _f) in roles[role].items():
            candidates.setdefault(k, cand)
    keys = list(candidates)
    blocked = 1e6
    # Пустой слот стоит 0: любая реальная оценка лучше дырки.
    cost = [[-(roles[role][k][1] * roles[role][k][2]) if k in roles[role] else blocked for k in keys] + [0.0] * len(slots)
            for _code, role, _order in slots]
    assignment = _hungarian(cost) if slots else []
    taken, total, by_slot = set(), 0.0, {}
    chosen = {}
    for row, ((code, role, _order), col) in enumerate(zip(slots, assignment)):
        if col < len(keys) and cost[row][col] < blocked:
            chosen[code] = keys[col]
            taken.add(keys[col])
            total += -cost[row][col]  # с коэффициентом: игра не на своей позиции стоит меньше
    for code, role, _order in slots:
        others = sorted(((c, s) for k, (c, s, _f) in roles[role].items() if k not in taken),
                        key=lambda pair: pair[1], reverse=True)
        if code in chosen:
            cand, score, _f = roles[role][chosen[code]]
            by_slot[code] = [(cand, score)] + others
        else:
            by_slot[code] = []
    return by_slot, slots, round(total, 2)


# Бонус к сумме рейтингов за популярность: схема всех команд тура получает +1 балл (на 11 игроков).
# Без него выбор прыгает из-за десятых и побеждают экзотические схемы одной команды.
POPULARITY_WEIGHT = 1.0


def choose_formation(pool_by_code: dict, rank, key, used: Counter, extra: tuple = ()) -> FormationChoice:
    """Лучшая из сыгранных схем. used — {схема: сколько раз ей выходили команды}; extra — кандидаты сверх used."""
    roles = _role_rankings(pool_by_code, rank, key)
    lineups = sum(used.values())
    names = list(dict.fromkeys([n for n in used if n in FORMATIONS] + [n for n in extra if n in FORMATIONS]))
    names = names or [DEFAULT_FORMATION]
    results = []
    for name in names:
        by_slot, slots, total = _fill(name, roles, key)
        results.append(FormationChoice(name, by_slot, slots, total, used.get(name, 0)))

    def merit(r):
        return r.total + (POPULARITY_WEIGHT * r.used / lineups if lineups else 0)

    results.sort(key=lambda r: (round(merit(r), 2), r.used), reverse=True)
    best = results[0]
    best.alternatives = [(r.name, r.total) for r in results[1:]]
    best.note = _note(best, lineups)
    return best


def _note(choice: FormationChoice, lineups: int) -> str:
    """Почему эта схема — для подсказки на странице."""
    if not choice.total:
        return ""
    from core.templatetags.ui_extras import ru_plural

    text = f"Схема {choice.name} подобрана по составу: сумма рейтингов одиннадцати игроков — {ru_num(choice.total, 1)}"
    if choice.alternatives:
        runner, runner_total = choice.alternatives[0]
        if runner_total > choice.total:
            text += f"; у {runner} чуть больше ({ru_num(runner_total, 1)}), но так команды играли реже"
        else:
            text += f", у {runner} — {ru_num(runner_total, 1)}"
    text += "."
    if choice.used and lineups:
        text += f" Так команды выходили {choice.used} {ru_plural(choice.used, 'раз,раза,раз')} из {lineups}."
    return text
