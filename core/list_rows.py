# core/list_rows.py
"""Готовые строки для списков игроков, тренеров, судей и команд (components/_entity_row.html)."""
from __future__ import annotations

from django.urls import reverse

from core.templatetags.rating_extras import rating_tone
from core.templatetags.ui_extras import ru_plural


def _rating(value) -> tuple[str, str]:
    if value is None:
        return "", ""
    return f"{value:.1f}".replace(".", ","), rating_tone(value)


def _initials(first: str, last: str) -> str:
    return f"{(first or ' ')[0]}{(last or ' ')[0]}".strip().upper()


def _count(n: int, forms: str) -> str:
    return f"{n} {ru_plural(n, forms)}"


def player_row(p) -> dict:
    from players.positions import position_label

    parts = [p.team.name if p.team_id else "Без команды", position_label(p.position) or ""]
    if p.number:
        parts.append(f"№{p.number}")
    rating = (getattr(p, "rating", None) or {}).get("rating")
    value, tone = _rating(rating)
    return {
        "url": reverse("players:detail", args=[p.id]), "img": p.photo_display, "initials": _initials(p.first_name, p.last_name),
        "title": f"{p.first_name} {p.last_name}".strip(), "sub": " · ".join(x for x in parts if x),
        "tag": "" if p.is_active else "покинул клуб", "value": value, "tone": tone,
        "value_label": "Средний рейтинг болельщиков за сезон",
        "meta": _count(getattr(p, "total_matches", 0) or 0, "матч,матча,матчей"),
    }


def coach_row(c) -> dict:
    r = getattr(c, "rating", None) or {}
    value, tone = _rating(r.get("rating"))
    return {
        "url": reverse("coaches:detail", args=[c.id]), "img": "", "initials": _initials(c.first_name, c.last_name),
        "title": f"{c.first_name} {c.last_name}".strip(), "sub": c.team.name if c.team_id else "Без команды",
        "value": value, "tone": tone, "value_label": "Средняя оценка работы тренера",
        "meta": _count(r.get("votes") or 0, "оценка,оценки,оценок") if r else "",
    }


def referee_row(ref) -> dict:
    quality = getattr(ref, "avg_decision_quality", None)
    value, tone = _rating(quality)
    matches = getattr(ref, "total_matches", 0) or 0
    influence = getattr(ref, "avg_influence", None)
    sub = _count(matches, "матч,матча,матчей")
    if influence is not None:
        sub += f" · влияние {influence:.0f}"
    return {
        "url": reverse("referees:detail", args=[ref.id]), "img": "", "initials": _initials(ref.first_name, ref.last_name),
        "title": f"{ref.first_name} {ref.last_name}".strip(), "sub": sub,
        "value": value, "tone": tone, "value_label": "Качество решений по оценкам болельщиков",
    }


def team_row(t) -> dict:
    r = getattr(t, "rating", None) or {}
    value, tone = _rating(r.get("rating"))
    standing = getattr(t, "standing", None)
    matches = (getattr(t, "home_matches_count", 0) or 0) + (getattr(t, "away_matches_count", 0) or 0)
    sub = (f"{standing.position}-е место · {_count(standing.points, 'очко,очка,очков')}"
           if standing and standing.played else _count(matches, "матч,матча,матчей"))
    return {
        "url": reverse("teams:detail", args=[t.id]), "img": t.logo_display, "crest": True,
        "initials": (t.name or "?")[0].upper(), "title": t.name, "sub": sub,
        "value": value, "tone": tone, "value_label": "Средняя оценка команды болельщиками",
    }
