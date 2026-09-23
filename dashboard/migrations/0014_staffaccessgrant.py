# dashboard/migrations/0014_staffaccessgrant.py
# Ручная миграция (см. докстринг 0001_initial — нет сетевого доступа к
# Postgres в песочнице, makemigrations не запустить). Создаёт StaffAccessGrant
# (2026-09-23, раздел «Роли доступа») — см. dashboard/models.py::
# StaffAccessGrant/DASHBOARD_SECTIONS.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('dashboard', '0013_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.CreateModel(
            name='StaffAccessGrant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('allowed_sections', models.JSONField(blank=True, default=list, help_text='Ключи из DASHBOARD_SECTIONS — какие вкладки дашборда видит и может открыть этот сотрудник', verbose_name='Разрешённые разделы')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Изменено')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Кто настраивал')),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='dashboard_access_grant', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Права доступа сотрудника',
                'verbose_name_plural': 'Права доступа сотрудников',
            },
        ),
    ]
