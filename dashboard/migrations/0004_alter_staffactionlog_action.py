# dashboard/migrations/0004_alter_staffactionlog_action.py
# Ручная миграция (тот же паттерн, что 0002/0003 — Django недоступен в
# этой песочнице для makemigrations). Добавляет SPORTMONKS_HEALTH_CHECK
# (dashboard/views.py::parser_sportmonks_health_check, 2026-09-08, финальная
# полировка staff-дашборда после cutover на Sportmonks — см. ADR-0044) в
# choices поля action. На схему БД не влияет (CharField, choices не
# enforced на уровне БД), только на state миграций для
# `makemigrations --check --dry-run` в CI.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0003_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('raw_kff_lookup', 'Просмотр сырого ответа KFF API'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('kff_health_check', 'Проверка доступности KFF API'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
