# notifications/styles.py
"""Вид уведомлений: иконка, цвет и раздел ленты по типу. Одно место для колокольчика и страницы."""
from __future__ import annotations

# тип -> (иконка Tabler, цвет daisyUI, раздел)
TYPE_STYLE: dict[str, tuple[str, str, str]] = {
    "match_started": ("ti-player-play", "success", "matches"),
    "match_event": ("ti-ball-football", "error", "matches"),
    "match_finished": ("ti-flag-3", "success", "matches"),
    "lineups_available": ("ti-clipboard-list", "primary", "matches"),
    "match_changed": ("ti-calendar-event", "warning", "matches"),
    "voting_open": ("ti-dopx-rate", "primary", "matches"),
    "voting_closing": ("ti-hourglass", "warning", "matches"),
    "evaluation_reminder": ("ti-hourglass", "warning", "matches"),
    "ratings_published": ("ti-chart-bar", "accent", "matches"),
    "round_results": ("ti-star", "accent", "matches"),
    "top_performance": ("ti-medal", "accent", "matches"),
    "aggregate_updated": ("ti-chart-line", "info", "matches"),
    "prediction_closing": ("ti-clock", "warning", "predictions"),
    "prediction_result": ("ti-target-arrow", "accent", "predictions"),
    "new_badge": ("ti-award", "info", "progress"),
    "level_up": ("ti-trending-up", "warning", "progress"),
    "weekly_digest": ("ti-calendar-stats", "info", "progress"),
    "contact_reply": ("ti-message-check", "success", "service"),
    "verification_required": ("ti-mail-exclamation", "warning", "service"),
    "welcome": ("ti-confetti", "primary", "service"),
    "system": ("ti-info-circle", "neutral", "service"),
}
DEFAULT_STYLE = ("ti-bell", "neutral", "service")

# Разделы ленты в порядке вкладок.
SECTIONS: list[tuple[str, str]] = [
    ("matches", "Матчи"),
    ("predictions", "Прогнозы"),
    ("progress", "Достижения"),
    ("service", "Сервис"),
]


def style_for(notification_type: str) -> dict:
    icon, color, section = TYPE_STYLE.get(notification_type, DEFAULT_STYLE)
    return {"icon": icon, "color": color, "section": section}


def types_in(section: str) -> list[str]:
    """Типы раздела; «Сервис» — ещё и все неизвестные типы (исключаются известные других разделов)."""
    return [t for t, (_i, _c, s) in TYPE_STYLE.items() if s == section]
