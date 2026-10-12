# players/positions.py
"""Нормализация позиций игроков; схемы сборных — players.formations.

clean_position_code() — при записи: только регистр/пробелы, данные не теряем.
position_label() — при отображении: None для неизвестного кода (шаблон покажет «—»).
"""
from __future__ import annotations

# Ключи — код после clean_position_code().
POSITION_LABELS: dict[str, str] = {
    "GK": "Вратарь",
    "DF": "Защитник",
    # «D» — основной код защитника в данных.
    "D": "Защитник",
    "CB": "Центральный защитник",
    "LB": "Левый защитник",
    "RB": "Правый защитник",
    "LWB": "Левый латераль",
    "RWB": "Правый латераль",
    "DM": "Опорный полузащитник",
    "CM": "Центральный полузащитник",
    "M": "Полузащитник",
    "MF": "Полузащитник",
    "AM": "Атакующий полузащитник",
    "LM": "Левый полузащитник",
    "RM": "Правый полузащитник",
    "WM": "Крайний полузащитник",
    "F": "Нападающий",
    "FW": "Нападающий",
    "ST": "Нападающий",
    "CF": "Центральный нападающий",
    "W": "Вингер",
    "LW": "Левый вингер",
    "RW": "Правый вингер",
}


# label -> [коды] для фильтра по позиции (уникальные подписи).
LABEL_TO_CODES: dict[str, list[str]] = {}
for _code, _label in POSITION_LABELS.items():
    LABEL_TO_CODES.setdefault(_label, []).append(_code)


def clean_position_code(raw: str | None) -> str:
    """Нормализация регистра/пробелов. Неизвестные коды сохраняем как есть."""
    if not raw:
        return ""
    return raw.strip().upper()


def position_label(code: str | None) -> str | None:
    """Подпись позиции или None для неизвестного кода."""
    if not code:
        return None
    return POSITION_LABELS.get(code.strip().upper())


# ---------------------------------------------------------------------------
# Координаты для мини-схемы поля в профиле игрока.
# Атака сверху (y=0), свои ворота снизу (y=100), x — слева направо.
PITCH_COORDS: dict[str, tuple[int, int]] = {
    "GK": (50, 93),
    "LB": (12, 78), "LWB": (12, 71),
    "CB": (50, 78), "DF": (50, 78), "D": (50, 78),
    "RB": (88, 78), "RWB": (88, 71),
    "DM": (50, 61),
    "CM": (50, 46), "MF": (50, 46), "M": (50, 46),
    "LM": (18, 46),
    "RM": (82, 46),
    "AM": (50, 30),
    "LW": (15, 21),
    "RW": (85, 21),
    "W": (50, 17),
    "CF": (50, 12), "ST": (50, 12), "F": (50, 12), "FW": (50, 12),
}


# Группа позиции — для цвета маркера на мини-схеме.
_POSITION_GROUP: dict[str, str] = {
    "GK": "gk",
    "LB": "def", "LWB": "def", "CB": "def", "DF": "def", "D": "def",
    "RB": "def", "RWB": "def",
    "DM": "mid", "CM": "mid", "MF": "mid", "M": "mid",
    "LM": "mid", "RM": "mid", "AM": "mid",
    "LW": "fwd", "RW": "fwd", "W": "fwd",
    "CF": "fwd", "ST": "fwd", "F": "fwd", "FW": "fwd",
}


# Коды без стороны (D/M/AM/F) + зона L/R из field_position -> боковой эквивалент.
# DM не трогаем — DM:L/DM:R только для разделения CM1/CM2.
_ZONE_SIDE_EQUIVALENT: dict[str, dict[str, str]] = {
    "D": {"L": "LB", "R": "RB"}, "DF": {"L": "LB", "R": "RB"}, "CB": {"L": "LB", "R": "RB"},
    "M": {"L": "LM", "R": "RM"}, "CM": {"L": "LM", "R": "RM"}, "MF": {"L": "LM", "R": "RM"},
    "AM": {"L": "LW", "R": "RW"},
    "F": {"L": "LW", "R": "RW"}, "FW": {"L": "LW", "R": "RW"},
    "CF": {"L": "LW", "R": "RW"}, "ST": {"L": "LW", "R": "RW"},
}


def player_position_display_code(amplua: str | None, field_position: str | None) -> str:
    """Код для мини-схемы с учётом зоны из field_position. Пустая строка, если амплуа неизвестно."""
    code = clean_position_code(amplua)
    if not code:
        return ""
    zone = _FIELD_POSITION_ZONE.get(clean_position_code(field_position), "")
    # Детальную позицию (CB, CM, AM, CF…) зона из угадывания по расстановке не меняет — только грубую (D/M/F).
    if zone in ("L", "R") and code in _COARSE_CODES:
        side_map = _ZONE_SIDE_EQUIVALENT.get(code)
        if side_map and zone in side_map:
            return side_map[zone]
    return code


# Грубые коды старых записей (без детальной позиции).
_COARSE_CODES = {"D", "DF", "M", "MF", "F", "FW", "W"}


def player_position_breakdown(counts: dict[str, int]) -> list[dict]:
    """Точки для мини-схемы из {код: матчей}, по убыванию частоты. Коды без координат пропускаем.
    Грубый код («Полузащитник») вливается в самую частую детальную позицию той же линии, если она есть."""
    counts = {clean_position_code(k): v for k, v in counts.items() if v}
    for coarse in [c for c in counts if c in _COARSE_CODES]:
        group = _POSITION_GROUP.get(coarse)
        detailed = [c for c in counts if c not in _COARSE_CODES and _POSITION_GROUP.get(c) == group]
        if detailed:
            top = max(detailed, key=lambda c: counts[c])
            counts[top] += counts.pop(coarse)
    rows = []
    for raw_code, count in counts.items():
        if not count:
            continue
        code = clean_position_code(raw_code)
        coords = PITCH_COORDS.get(code)
        label = position_label(code)
        if not coords or not label:
            continue
        x, y = coords
        rows.append({
            "code": code, "label": label, "count": count, "x": x, "y": y,
            "group": _POSITION_GROUP.get(code, "mid"),
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


# L/R как есть, C/LC/RC -> C.
_FIELD_POSITION_ZONE: dict[str, str] = {
    "L": "L",
    "R": "R",
    "C": "C",
    "LC": "C",
    "RC": "C",
}


# Детальная позиция сама задаёт сторону — зону из угадывания по расстановке для неё не берём
# (иначе «левый защитник справа»). Коды — те, что принимают амплуа players.formations.ROLE_CODES.
_DETAILED_SLOT_CODE: dict[str, str] = {
    "GK": "GK:C",
    "CB": "D:C", "LB": "D:L", "RB": "D:R", "LWB": "D:L", "RWB": "D:R",
    "DM": "DM:C", "CM": "M:C", "AM": "AM:C",
    "LM": "M:L", "RM": "M:R", "LW": "AM:L", "RW": "AM:R",
    "CF": "F:C", "ST": "F:C",
}


def resolve_lineup_codes(amplua: str | None, field_position: str | None) -> list[str]:
    """Код для pool_by_code: детальная позиция -> код слота; грубая -> ['AMPLUA:ZONE'] или ['AMPLUA'];
    пустой список для неизвестного амплуа."""
    amplua_code = clean_position_code(amplua)
    if not amplua_code:
        return []
    if amplua_code in _DETAILED_SLOT_CODE:
        return [_DETAILED_SLOT_CODE[amplua_code]]
    zone = _FIELD_POSITION_ZONE.get(clean_position_code(field_position), "")
    if zone:
        return [f"{amplua_code}:{zone}"]
    return [amplua_code]
