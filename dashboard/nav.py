# dashboard/nav.py
"""Состав меню дашборда: разделы -> страницы; ключ страницы = ключ доступа = active_tab."""
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
    hint: str = ""
    warn: bool = False


@dataclass(frozen=True)
class NavGroup:
    key: str
    icon: str
    label: str
    items: tuple[NavItem, ...]


NAV_GROUPS: tuple[NavGroup, ...] = (
    NavGroup("home", "ti-layout-dashboard", "Обзор", (
        NavItem("overview", "dashboard:overview", "ti-chart-line", "Обзор", "Главные цифры платформы"),
        NavItem("traffic", "dashboard:traffic", "ti-world", "Трафик", "Посещаемость и источники"),
    )),
    NavGroup("data", "ti-database", "Данные", (
        NavItem("matches", "dashboard:matches_list", "ti-ball-football", "Матчи", "Список матчей и ресинк"),
        NavItem("evaluation_sessions", "dashboard:evaluation_sessions_list", "ti-clipboard-check", "Оценки", "Модерация оценок"),
        NavItem("data_health", "dashboard:data_health", "ti-activity-heartbeat", "Здоровье данных", "Пропуски составов и событий"),
        NavItem("data_trust", "dashboard:data_trust", "ti-shield-check", "Доверие к данным", "Расхождения источников"),
        NavItem("duplicate_players", "dashboard:duplicate_players_review", "ti-users-group", "Дубли игроков", "Слияние одинаковых игроков"),
        NavItem("names_review", "dashboard:names_review", "ti-sparkles", "Проверка ФИО", "Правки имён от ИИ"),
        NavItem("parser_tools", "dashboard:parser_tools", "ti-server-cog", "Парсер", "Sportmonks и синхронизация"),
    )),
    NavGroup("people", "ti-users", "Люди", (
        NavItem("users", "dashboard:users_list", "ti-users", "Пользователи", "Аккаунты и активность"),
        NavItem("antifraud", "dashboard:antifraud", "ti-shield-exclamation", "Антифрод", "Подозрительные голоса"),
        NavItem("audit", "dashboard:audit_log", "ti-history", "Аудит", "Действия сотрудников"),
    )),
    NavGroup("content", "ti-speakerphone", "Контент", (
        NavItem("experts", "dashboard:experts", "ti-microphone", "Эксперты", "Мнения о матчах"),
        NavItem("channel", "dashboard:channel", "ti-brand-telegram", "Telegram-канал", "Посты, автопостинг, очередь"),
        NavItem("social_content", "dashboard:social_content", "ti-photo-share", "Соцсети", "Готовые посты тура"),
        NavItem("announcements", "dashboard:announcements", "ti-speakerphone", "Объявления", "Рассылка всем пользователям"),
        NavItem("ads", "dashboard:ads", "ti-ad-2", "Реклама", "Партнёры, баннеры, виджеты"),
        NavItem("mourning", "dashboard:mourning", "ti-ribbon-health", "Траур", "Траурный режим сайта"),
    )),
    NavGroup("system", "ti-adjustments", "Система", (
        NavItem("system_status", "dashboard:system_status", "ti-heart-rate-monitor", "Статус", "Сервисы, версия, деплои"),
        NavItem("platform_settings", "dashboard:platform_settings", "ti-adjustments", "Настройки", "Параметры платформы"),
        NavItem("scripts", "dashboard:scripts", "ti-terminal-2", "Скрипты", "Команды обслуживания", warn=True),
        NavItem("admin_bot", "dashboard:admin_bot", "ti-brand-telegram", "Telegram-бот", "Привязка и состояние бота"),
        NavItem("access_roles", "dashboard:access_roles_list", "ti-user-shield", "Доступы", "Сотрудники и роли"),
    )),
)


def _can_open(user, item: NavItem) -> bool:
    """Раздел открыт и хватает прав на просмотр его страницы (dashboard/permissions.py)."""
    from .permissions import missing_perms

    return user_can_access_section(user, item.key) and not missing_perms(user, item.url_name.split(":")[1], "GET")


def build_nav(user, active_tab: str = "") -> dict:
    """Видимые пользователю разделы и страницы; пустые разделы выброшены."""
    groups = []
    for group in NAV_GROUPS:
        items = [
            {"key": i.key, "url": reverse(i.url_name), "icon": i.icon, "label": i.label, "hint": i.hint,
             "warn": i.warn, "active": i.key == active_tab}
            for i in group.items if _can_open(user, i)
        ]
        if items:
            groups.append({"key": group.key, "icon": group.icon, "label": group.label, "items": items,
                           "url": items[0]["url"], "active": any(i["active"] for i in items)})
    current = next((g for g in groups if g["active"]), None)
    return {
        "groups": groups,
        "current": current,
        "total": sum(len(g["items"]) for g in groups),
        "show_scripts": user_can_access_section(user, "scripts"),
        # /admin/ без прав показывает пустую страницу — иконку прячем.
        "show_admin": user.is_superuser or bool(user.get_all_permissions()),
    }


def first_allowed_url(user) -> str | None:
    """Первый открытый сотруднику раздел дашборда — куда вести со страницы 403."""
    for group in NAV_GROUPS:
        for item in group.items:
            if _can_open(user, item):
                return reverse(item.url_name)
    return None
