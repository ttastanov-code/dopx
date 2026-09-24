# dashboard/nav.py
"""Состав меню дашборда. Ряды -> группы -> вкладки; ключ вкладки = ключ раздела = active_tab."""
from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from .access import user_can_access_section


@dataclass(frozen=True)
class NavItem:
    key: str
    url_name: str
    icon: str
    label: str


NAV_ROWS: tuple[tuple[tuple[NavItem, ...], ...], ...] = (
    (
        (
            NavItem("overview", "dashboard:overview", "ti-chart-line", "Обзор"),
            NavItem("traffic", "dashboard:traffic", "ti-world", "Трафик"),
            NavItem("matches", "dashboard:matches_list", "ti-ball-football", "Матчи"),
        ),
        (
            NavItem("data_health", "dashboard:data_health", "ti-activity-heartbeat", "Здоровье данных"),
            NavItem("data_trust", "dashboard:data_trust", "ti-shield-check", "Доверие к данным"),
            NavItem("duplicate_players", "dashboard:duplicate_players_review", "ti-users-group", "Дубли игроков"),
            NavItem("names_review", "dashboard:names_review", "ti-sparkles", "Проверка ФИО"),
            NavItem("evaluation_sessions", "dashboard:evaluation_sessions_list", "ti-clipboard-check", "Оценки"),
        ),
    ),
    (
        (
            NavItem("users", "dashboard:users_list", "ti-users", "Пользователи"),
            NavItem("antifraud", "dashboard:antifraud", "ti-shield-exclamation", "Антифрод"),
            NavItem("parser_tools", "dashboard:parser_tools", "ti-server-cog", "Парсер"),
            NavItem("ads", "dashboard:ads", "ti-code", "Реклама"),
        ),
        (
            NavItem("audit", "dashboard:audit_log", "ti-history", "Аудит"),
            NavItem("announcements", "dashboard:announcements", "ti-speakerphone", "Объявления"),
            NavItem("platform_settings", "dashboard:platform_settings", "ti-adjustments", "Настройки"),
            NavItem("system_status", "dashboard:system_status", "ti-heart-rate-monitor", "Статус"),
        ),
    ),
)

# Больше стольких вкладок — два ряда, иначе один.
SINGLE_ROW_MAX_ITEMS = 9


def build_nav(user) -> dict:
    """Видимые пользователю вкладки: пустые группы и ряды выброшены."""
    rows = []
    for row in NAV_ROWS:
        groups = []
        for group in row:
            items = [
                {"key": i.key, "url": reverse(i.url_name), "icon": i.icon, "label": i.label}
                for i in group if user_can_access_section(user, i.key)
            ]
            if items:
                groups.append(items)
        if groups:
            rows.append(groups)

    total = sum(len(g) for row in rows for g in row)
    if total <= SINGLE_ROW_MAX_ITEMS and len(rows) > 1:
        rows = [[g for row in rows for g in row]]
    return {
        "rows": rows,
        "total": total,
        "show_scripts": user_can_access_section(user, "scripts"),
        # /admin/ без прав показывает пустую страницу — иконку прячем.
        "show_admin": user.is_superuser or bool(user.get_all_permissions()),
    }


def first_allowed_url(user) -> str | None:
    """Первый открытый сотруднику раздел дашборда — куда вести со страницы 403."""
    for row in NAV_ROWS:
        for group in row:
            for item in group:
                if user_can_access_section(user, item.key):
                    return reverse(item.url_name)
    if user_can_access_section(user, "scripts"):
        return reverse("dashboard:scripts")
    return None
