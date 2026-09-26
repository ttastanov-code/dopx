# Доступ к дашборду теперь запрещён без StaffAccessGrant — действующим сотрудникам сохраняем полный.
from django.conf import settings
from django.db import migrations

SECTION_KEYS = [
    "overview", "traffic", "matches", "data_health", "data_trust", "duplicate_players",
    "names_review", "evaluation_sessions", "users", "antifraud", "parser_tools", "ads",
    "audit", "announcements", "platform_settings", "system_status", "scripts",
]


def grant_full_access(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    StaffAccessGrant = apps.get_model("dashboard", "StaffAccessGrant")
    for user in User.objects.filter(is_staff=True, is_superuser=False, dashboard_access_grant__isnull=True):
        StaffAccessGrant.objects.create(user=user, allowed_sections=list(SECTION_KEYS))


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0017_alter_staffactionlog_action"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(grant_full_access, migrations.RunPython.noop),
    ]
