# dashboard/migrations/0006_alter_staffactionlog_action.py
# Тот же паттерн, что 0002-0005 (ручная миграция, Django недоступен в
# песочнице для makemigrations) — добавляет два новых choices действия
# для "Центра доверия к данным" (2026-09-09, see dashboard/models.py::
# AuditAction.STADIUM_MARKED_REVIEWED / DATA_ERROR_REPORT_RESOLVED). На
# схему БД не влияет (CharField, choices не enforced на уровне БД), только
# на state миграций для `makemigrations --check --dry-run`.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0005_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление'), ('stadium_marked_reviewed', 'Стадион отмечен проверенным'), ('data_error_report_resolved', 'Жалоба на данные матча закрыта')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
