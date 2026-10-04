# dashboard/views/__init__.py
"""Staff-дашборд. Вьюхи тонкие, агрегация — в services.py; разделы — по модулям пакета."""
from .overview import (  # noqa: F401
    overview,
    traffic,
    retention,
    OVERVIEW_DAY_PRESETS,
)
from .matches import (  # noqa: F401
    matches_list,
    match_detail,
    match_trigger_recalc,
    MATCHES_PAGE_SIZE,
)
from .settings import (  # noqa: F401
    platform_settings,
    platform_flags_save,
    platform_settings_create,
    platform_settings_update,
    platform_settings_delete,
)
from .moderation import (  # noqa: F401
    users_list,
    user_detail,
    user_toggle_ban,
    user_reset_trust_score,
    data_health,
    data_health_partial,
    _resolve_match_for_resync,
    data_health_resync_match,
    reports,
    report_action,
    antifraud,
    antifraud_flag_action,
    antifraud_export_csv,
    USERS_PAGE_SIZE,
)
from .trust import (  # noqa: F401
    data_trust,
    data_trust_resolve_report,
    data_trust_review_discrepancy,
)
from .parser import (  # noqa: F401
    parser_tools_view,
    parser_tasks_partial,
    parser_trigger_task,
    parser_sportmonks_health_check,
    sportmonks_sync_toggle,
    parser_revoke_task,
    SPORTMONKS_SYNC_ENABLED_KEY,
)
from .scripts import (  # noqa: F401
    _scripts_runs_page,
    scripts_view,
    scripts_runs_partial,
    scripts_trigger,
    scripts_revoke_run,
    SCRIPTS_RUNS_PAGE_SIZE,
)
from .names import (  # noqa: F401
    _names_review_queue_context,
    names_review,
    names_review_partial,
    names_review_bulk_confirm_matches,
    names_review_action,
    _player_dup_stats,
    duplicate_players_review,
    duplicate_players_merge,
    duplicate_players_dismiss,
    _duplicate_result,
)
from .ads import (  # noqa: F401
    _ads_stats_context,
    ads,
    ads_stats_partial,
)
from .system import (  # noqa: F401
    audit_log,
    announcements,
    evaluation_sessions_list,
    evaluation_session_detail,
    evaluation_session_delete,
    service_restart,
    system_status_services,
    system_status,
    EVALUATION_SESSIONS_PAGE_SIZE,
)
from .partners import (  # noqa: F401
    partners_list,
    partner_create,
    partner_detail,
    partner_delete,
    _banner_preview_url,
    _annotate_banners,
    banners_list,
    _render_banner_form,
    _banner_stats_context,
    banner_version,
    banner_stats_partial,
    banner_create,
    banner_detail,
    banner_toggle,
    banner_duplicate,
    _sandbox_banner,
    banner_sandbox,
    banner_sandbox_frame,
    banner_delete,
    PARTNERS_PAGE_SIZE,
    BANNERS_PAGE_SIZE,
    BANNER_STATUS_META,
    SANDBOX_DEMOS,
    SANDBOX_DEVICES,
)
from .content import (  # noqa: F401
    mourning_mode,
    social_content,
)
