# dashboard/nav_data.py
"""Состав меню дашборда без импорта моделей: читают и дашборд (nav.py), и сайдбар админки (dopx/admin_nav.py)."""
from __future__ import annotations

from dataclasses import dataclass


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
        NavItem("reports", "dashboard:reports", "ti-flag", "Жалобы", "Ники, аватары, лиги"),
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
        NavItem("server", "dashboard:server", "ti-server-2", "Сервер", "Логи, кэш, очереди, база, бэкапы, обслуживание", warn=True),
        NavItem("platform_settings", "dashboard:platform_settings", "ti-adjustments", "Настройки", "Параметры платформы"),
        NavItem("scripts", "dashboard:scripts", "ti-terminal-2", "Скрипты", "Команды обслуживания", warn=True),
        NavItem("admin_bot", "dashboard:admin_bot", "ti-brand-telegram", "Telegram-бот", "Привязка и состояние бота"),
        NavItem("access_roles", "dashboard:access_roles_list", "ti-user-shield", "Доступы", "Сотрудники и роли"),
    )),
)
