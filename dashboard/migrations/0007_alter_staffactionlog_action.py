# dashboard/migrations/0007_alter_staffactionlog_action.py
# Тот же паттерн, что 0002-0006 (ручная миграция, Django недоступен в
# песочнице для makemigrations) — убирает stadium_marked_reviewed (Stadium
# удалён из проекта целиком, 2026-09-09, решение пользователя) и добавляет
# parser_discrepancy_reviewed (расширенный Центр доверия к данным). На
# схему БД не влияет (CharField, choices не enforced на уровне БД), только
# на state миграций для `makemigrations --check --dry-run`. Существующие
# строки StaffActionLog со значением 'stadium_marked_reviewed' в БД не
# трогаются (см. комментарий в dashboard/models.py::AuditAction).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0006_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление'), ('data_error_report_resolved', 'Жалоба на данные матча закрыта'), ('parser_discrepancy_reviewed', 'Расхождение импорта разобрано')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
