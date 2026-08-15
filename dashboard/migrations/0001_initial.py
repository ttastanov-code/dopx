# Generated manually — в песочнице разработки нет сетевого доступа к
# PyPI/Postgres, поэтому `manage.py makemigrations` тут не запустить. Файл
# написан вручную по образцу analytics/migrations/0001_initial.py; поля и
# индексы 1:1 совпадают с dashboard/models.py. Перед мёржем в основную ветку
# рекомендуется прогнать `python manage.py makemigrations --check` на
# машине с рабочим окружением.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='StaffActionLog',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Когда')),
                ('actor_username', models.CharField(blank=True, max_length=150, verbose_name='Логин (снимок)')),
                ('action', models.CharField(choices=[
                    ('antifraud_flag_confirmed', 'Флаг подтверждён'),
                    ('antifraud_flag_dismissed', 'Флаг отклонён'),
                    ('match_resync', 'Ручной ресинк матча'),
                    ('celery_task_triggered', 'Запуск celery-задачи вручную'),
                    ('raw_kff_lookup', 'Просмотр сырого ответа KFF API'),
                ], db_index=True, max_length=50, verbose_name='Действие')),
                ('target', models.CharField(blank=True, max_length=300, verbose_name='Объект действия')),
                ('details', models.JSONField(blank=True, default=dict, verbose_name='Детали')),
                ('ip_address', models.GenericIPAddressField(blank=True, null=True, verbose_name='IP')),
                ('actor', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='staff_action_logs',
                    to=settings.AUTH_USER_MODEL,
                    verbose_name='Кто',
                )),
            ],
            options={
                'verbose_name': 'Запись аудита staff',
                'verbose_name_plural': 'Аудит-лог staff',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='staffactionlog',
            index=models.Index(fields=['action', 'created_at'], name='staff_audit_action_time_idx'),
        ),
        migrations.AddIndex(
            model_name='staffactionlog',
            index=models.Index(fields=['actor', 'created_at'], name='staff_audit_actor_time_idx'),
        ),
    ]
