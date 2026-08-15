# dashboard/urls.py
from django.urls import path

from . import views, views_2fa

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("traffic/", views.traffic, name="traffic"),
    path("data-health/", views.data_health, name="data_health"),
    path("data-health/matches/<uuid:match_id>/resync/", views.data_health_resync_match, name="data_health_resync_match"),
    path("antifraud/", views.antifraud, name="antifraud"),
    path("antifraud/export/", views.antifraud_export_csv, name="antifraud_export_csv"),
    path("antifraud/flags/<uuid:flag_id>/action/", views.antifraud_flag_action, name="antifraud_flag_action"),
    path("parser/", views.parser_tools_view, name="parser_tools"),
    path("parser/trigger/", views.parser_trigger_task, name="parser_trigger_task"),
    path("audit/", views.audit_log, name="audit_log"),
    # 2FA (security-стек) — ЭТИ пути освобождены от самой OTP-проверки в
    # EXEMPT_PATH_PREFIXES (dashboard/middleware.py), иначе замкнутый круг.
    path("security/2fa/setup/", views_2fa.two_factor_setup, name="two_factor_setup"),
    path("security/2fa/backup-codes/", views_2fa.two_factor_backup_codes, name="two_factor_backup_codes"),
    path("security/2fa/verify/", views_2fa.two_factor_challenge, name="two_factor_challenge"),
]
