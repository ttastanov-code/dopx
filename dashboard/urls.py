# dashboard/urls.py
from django.urls import path

from . import views, views_2fa

app_name = "dashboard"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("traffic/", views.traffic, name="traffic"),
    path("data-health/", views.data_health, name="data_health"),
    path("data-health/partial/", views.data_health_partial, name="data_health_partial"),
    # НЕ <uuid:match_id> (см. докстринг views.data_health_resync_match про
    # причину 404 2026-09-22) — эта кнопка получает id из ДВУХ разных
    # источников: свежие ("Матчи с пропущенными данными") всегда шлют
    # настоящий UUID (Match.id), а "Последние ошибки" читает сырой JSON
    # ParserSyncRun.error_samples, где ещё живут строки времён удалённого
    # KFF-парсера с ЧИСЛОВЫМ id матча вместо UUID — <uuid:...> отбраковывал
    # такой запрос ДО того, как он вообще доходил до view (роутинг-404, а
    # не 404 из get_object_or_404), само тело view при этом ни разу не
    # запускалось.
    path("data-health/matches/<str:match_id>/resync/", views.data_health_resync_match, name="data_health_resync_match"),
    path("ads/", views.ads, name="ads"),
    path("ads/stats/partial/", views.ads_stats_partial, name="ads_stats_partial"),
    path("antifraud/", views.antifraud, name="antifraud"),
    path("antifraud/export/", views.antifraud_export_csv, name="antifraud_export_csv"),
    path("antifraud/flags/<uuid:flag_id>/action/", views.antifraud_flag_action, name="antifraud_flag_action"),
    path("data-trust/", views.data_trust, name="data_trust"),
    path("data-trust/reports/<uuid:submission_id>/resolve/", views.data_trust_resolve_report, name="data_trust_resolve_report"),
    path("data-trust/discrepancies/<uuid:discrepancy_id>/review/", views.data_trust_review_discrepancy, name="data_trust_review_discrepancy"),
    path("parser/", views.parser_tools_view, name="parser_tools"),
    path("parser/tasks/partial/", views.parser_tasks_partial, name="parser_tasks_partial"),
    path("parser/trigger/", views.parser_trigger_task, name="parser_trigger_task"),
    path("parser/sportmonks-health-check/", views.parser_sportmonks_health_check, name="parser_sportmonks_health_check"),
    path("parser/tasks/<str:task_id>/revoke/", views.parser_revoke_task, name="parser_revoke_task"),
    path("audit/", views.audit_log, name="audit_log"),
    path("announcements/", views.announcements, name="announcements"),
    # 2FA (security-стек) — ЭТИ пути освобождены от самой OTP-проверки в
    # EXEMPT_PATH_PREFIXES (dashboard/middleware.py), иначе замкнутый круг.
    path("security/2fa/setup/", views_2fa.two_factor_setup, name="two_factor_setup"),
    path("security/2fa/backup-codes/", views_2fa.two_factor_backup_codes, name="two_factor_backup_codes"),
    path("security/2fa/verify/", views_2fa.two_factor_challenge, name="two_factor_challenge"),
]
