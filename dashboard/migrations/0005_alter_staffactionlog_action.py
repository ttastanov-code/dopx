# dashboard/migrations/0005_alter_staffactionlog_action.py
# Ручная миграция (тот же паттерн, что 0002/0003/0004 — Django недоступен
# в этой песочнице для makemigrations). Убирает RAW_KFF_LOOKUP/
# KFF_HEALTH_CHECK из choices поля action — KFF-парсер физически удалён
# из проекта (2026-09-09, решение пользователя). На схему БД не влияет
# (CharField, choices не enforced на уровне БД), только на state миграций
# для `makemigrations --check --dry-run` в CI. Существующие строки
# StaffActionLog с этими значениями в БД не трогаются (см. комментарий в
# dashboard/models.py::AuditAction).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0004_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
