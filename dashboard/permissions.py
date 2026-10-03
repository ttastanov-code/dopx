# dashboard/permissions.py
"""Права на модели по URL дашборда: раздел решает, видна ли страница, права — что можно менять.
Каждый URL должен быть в VIEW_PERMS или SECTION_ONLY (проверяет тест)."""
from __future__ import annotations


def _p(get=(), post=None):
    """Права на GET и POST; post=None — те же, что на GET."""
    get = tuple(get) if not isinstance(get, str) else (get,)
    if post is None:
        post = get
    post = tuple(post) if not isinstance(post, str) else (post,)
    return {"GET": get, "POST": post}


VIEW_PERMS: dict[str, dict[str, tuple[str, ...]]] = {
    # Матчи
    "match_detail": _p(post="matches.change_match"),
    "match_trigger_recalc": _p("matches.change_match"),
    "data_health_resync_match": _p("matches.change_match"),
    # Настройки платформы
    "platform_settings": _p("core.view_platformsetting"),
    "platform_settings_create": _p("core.add_platformsetting"),
    "platform_flags_save": _p("core.change_platformsetting"),
    "platform_settings_update": _p("core.change_platformsetting"),
    "platform_settings_delete": _p("core.delete_platformsetting"),
    "sportmonks_sync_toggle": _p("core.change_platformsetting"),
    "mourning": _p("core.view_mourningmode", post="core.change_mourningmode"),
    # Пользователи и антифрод
    "users_list": _p("users.view_user"),
    "user_detail": _p("users.view_user"),
    "user_toggle_ban": _p("users.change_user"),
    "user_reset_trust_score": _p("users.change_user"),
    "antifraud": _p("users.view_suspiciousactivityflag"),
    "antifraud_export_csv": _p("users.view_suspiciousactivityflag"),
    "antifraud_flag_action": _p("users.change_suspiciousactivityflag"),
    "announcements": _p(post="notifications.add_notification"),
    "reports": _p("users.view_userreport"),
    "report_action": _p("users.change_userreport"),
    # Оценки
    "evaluation_sessions_list": _p("evaluations.view_evaluationsession"),
    "evaluation_session_detail": _p("evaluations.view_evaluationsession"),
    "evaluation_session_delete": _p("evaluations.delete_evaluationsession"),
    # Данные
    "data_trust_resolve_report": _p("notifications.change_contactsubmission"),
    "data_trust_review_discrepancy": _p("parsers.change_parserdiscrepancy"),
    "names_review": _p("parsers.view_nameverificationsuggestion"),
    "names_review_partial": _p("parsers.view_nameverificationsuggestion"),
    "names_review_action": _p(("parsers.change_nameverificationsuggestion", "parsers.add_confirmednamecorrection")),
    "names_review_bulk_confirm_matches": _p(("parsers.change_nameverificationsuggestion",
                                             "parsers.add_confirmednamecorrection")),
    "duplicate_players_review": _p("players.view_potentialduplicateplayer"),
    "duplicate_players_merge": _p(("players.change_player", "players.delete_player",
                                   "players.change_potentialduplicateplayer")),
    "duplicate_players_dismiss": _p("players.change_potentialduplicateplayer"),
    # Реклама
    "partners_list": _p("partners.view_partner"),
    "partner_create": _p("partners.add_partner"),
    "partner_detail": _p("partners.view_partner", post="partners.change_partner"),
    "partner_delete": _p("partners.delete_partner"),
    "banners_list": _p("partners.view_banner"),
    "banner_sandbox": _p("partners.view_banner"),
    "banner_sandbox_frame": _p("partners.view_banner"),
    "banner_stats_partial": _p("partners.view_banner"),
    "banner_version": _p("partners.view_banner"),
    "banner_create": _p("partners.add_banner"),
    "banner_detail": _p("partners.view_banner", post="partners.change_banner"),
    "banner_toggle": _p("partners.change_banner"),
    "banner_duplicate": _p("partners.add_banner"),
    "banner_delete": _p("partners.delete_banner"),
    # Эксперты
    "experts": _p("engagement.view_experttake"),
    "expert_take_players": _p("engagement.view_experttake"),
    "expert_take_create": _p("engagement.add_experttake"),
    "expert_take_edit": _p("engagement.view_experttake", post="engagement.change_experttake"),
    "expert_take_toggle": _p("engagement.change_experttake"),
    "expert_take_delete": _p("engagement.delete_experttake"),
    "expert_create": _p("engagement.add_expert"),
    "expert_edit": _p("engagement.view_expert", post="engagement.change_expert"),
    "expert_invite_create": _p("engagement.add_expertinvite"),
    "expert_invite_action": _p("engagement.change_expertinvite"),
    # Telegram-канал
    "channel": _p("adminbot.view_channelpost", post="adminbot.add_channelpost"),
    "channel_post": _p("adminbot.view_channelpost", post="adminbot.change_channelpost"),
    "channel_post_delete": _p("adminbot.delete_channelpost"),
    "channel_modes": _p("adminbot.delete_channelpost"),
}

# Страницы без своей модели (отчёты, задачи, 2FA): достаточно раздела.
# Роли доступа — только суперпользователь, проверка во вьюхах.
SECTION_ONLY = {
    "overview", "traffic", "retention", "matches_list", "system_status", "social_content",
    "data_health", "data_health_partial", "data_trust", "ads", "ads_stats_partial", "audit_log",
    "parser_tools", "parser_tasks_partial", "parser_trigger_task", "parser_sportmonks_health_check",
    "parser_revoke_task", "scripts", "scripts_runs_partial", "scripts_trigger", "scripts_revoke_run",
    "access_roles_list", "access_roles_detail", "access_revoke_staff",
    "access_grant_staff", "admin_groups_list", "admin_group_detail",
    "two_factor_setup", "two_factor_backup_codes", "two_factor_challenge",
    "admin_bot", "admin_bot_unlink", "service_restart", "system_status_services",
}


def required_perms(url_name: str, method: str) -> tuple[str, ...]:
    rule = VIEW_PERMS.get(url_name)
    if not rule:
        return ()
    return rule["GET"] if method in ("GET", "HEAD") else rule["POST"]


def missing_perms(user, url_name: str, method: str) -> list[str]:
    """Каких прав не хватает. Смотреть данные открытого раздела можно и без права view_ (раздел уже проверен)."""
    if getattr(user, "is_superuser", False):
        return []
    return [p for p in required_perms(url_name, method) if ".view_" not in p and not user.has_perm(p)]


def perm_label(perm: str) -> str:
    """«partners.change_banner» -> «Баннер: изменение» — для страницы отказа."""
    from django.apps import apps

    app_label, codename = perm.split(".", 1)
    action, _, model_name = codename.partition("_")
    try:
        verbose = apps.get_model(app_label, model_name)._meta.verbose_name
    except LookupError:
        verbose = model_name
    actions = {"view": "просмотр", "add": "создание", "change": "изменение", "delete": "удаление"}
    return f"{str(verbose).capitalize()}: {actions.get(action, action)}"
