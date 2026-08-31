# dashboard/migrations/0003_alter_staffactionlog_action.py
# Ручная миграция (тот же паттерн, что и users/migrations/0016 — Django в
# песочнице, где пишется код, недоступен, поэтому makemigrations нельзя
# прогнать локально; файл собран вручную по образцу 0002, но с полным
# choices списком AuditAction). Добавляет SYSTEM_ANNOUNCEMENT_SENT (staff-
# broadcast, dashboard/views.py::announcements, 2026-08-31) в choices поля
# action — на схему БД (CharField, choices не enforced на уровне БД) не
# влияет, только на state миграций, но CI (`makemigrations --check
# --dry-run`) требует, чтобы модель и миграции не расходились.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0002_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('raw_kff_lookup', 'Просмотр сырого ответа KFF API'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('kff_health_check', 'Проверка доступности KFF API'), ('system_announcement_sent', 'Отправлено системное объявление')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
