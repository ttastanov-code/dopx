# dashboard/migrations/0009_name_suggestion_audit_actions.py
# Ручная миграция — добавляет 2 новых choices для StaffActionLog.action
# (name_suggestion_approved/name_suggestion_rejected, см. dashboard/models.py::
# AuditAction) для очереди «Проверка ФИО (ИИ)» (parsers/models.py::
# NameVerificationSuggestion). На схему БД не влияет (CharField, choices не
# enforced на уровне БД), только на state миграций.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0008_managementcommandrun'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление'), ('data_error_report_resolved', 'Жалоба на данные матча закрыта'), ('parser_discrepancy_reviewed', 'Расхождение импорта разобрано'), ('management_command_triggered', 'Запуск management-команды из дашборда'), ('name_suggestion_approved', 'Предложение ИИ по ФИО подтверждено'), ('name_suggestion_rejected', 'Предложение ИИ по ФИО отклонено')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
