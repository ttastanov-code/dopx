# dashboard/admin_callback.py
"""UNFOLD["DASHBOARD_CALLBACK"]: главная /admin/ — вход к данным (таблицы с числом записей).
Мониторинг и инструменты — в staff-дашборде. Ошибки не пробрасываются — /admin/ не должен падать.
"""
from __future__ import annotations

import logging

from django.urls import NoReverseMatch, reverse

logger = logging.getLogger(__name__)

# Группы таблиц на главной админки: (заголовок, иконка, [(app_label, model_name, иконка)]).
DATA_SECTIONS = (
    ("Справочники", "category", (
        ("leagues", "league", "emoji_events"), ("seasons", "season", "calendar_month"), ("teams", "team", "groups"),
        ("players", "player", "sports"), ("coaches", "coach", "assignment_ind"), ("referees", "referee", "sports_score"),
    )),
    ("Матчи", "sports_soccer", (
        ("matches", "match", "sports_soccer"), ("lineups", "matchlineup", "assignment"), ("events", "matchevent", "bolt"),
        ("matches", "matchplayerstatistics", "leaderboard"), ("predictions", "matchprediction", "online_prediction"),
    )),
    ("Люди и обращения", "person", (
        ("users", "user", "person"), ("notifications", "contactsubmission", "mail"), ("users", "userreport", "flag"),
        ("users", "suspiciousactivityflag", "warning"), ("evaluations", "evaluationsession", "checklist"),
    )),
)


def _sections(request) -> list[dict]:
    from django.apps import apps
    from django.contrib import admin

    out = []
    for title, icon, models in DATA_SECTIONS:
        items = []
        for app_label, model_name, item_icon in models:
            try:
                model = apps.get_model(app_label, model_name)
                model_admin = admin.site._registry.get(model)
                if not model_admin or not model_admin.has_view_or_change_permission(request):
                    continue
                items.append({"title": str(model._meta.verbose_name_plural).capitalize(), "icon": item_icon,
                              "count": model._default_manager.count(),
                              "url": reverse(f"admin:{app_label}_{model_name}_changelist")})
            except (LookupError, NoReverseMatch):
                continue
        if items:
            out.append({"title": title, "icon": icon, "items": items})
    return out


def dashboard_callback(request, context):
    try:
        from django.contrib.admin.models import LogEntry

        context["dopx_sections"] = _sections(request)
        context["dopx_has_history"] = LogEntry.objects.exists()
    except Exception:
        logger.error("dashboard_callback: не удалось собрать разделы данных", exc_info=True)
    return context
