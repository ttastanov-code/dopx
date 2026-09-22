# dashboard/migrations/0008_managementcommandrun.py
# Ручная миграция (тот же паттерн, что 0002-0007 — Django недоступен в
# песочнице для makemigrations): новая модель ManagementCommandRun +
# расширение choices StaffActionLog.action новым значением
# 'management_command_triggered'. См. dashboard/models.py — раздел
# "Скрипты и команды" (2026-09-22, прямая просьба пользователя).
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('dashboard', '0007_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление'), ('data_error_report_resolved', 'Жалоба на данные матча закрыта'), ('parser_discrepancy_reviewed', 'Расхождение импорта разобрано'), ('management_command_triggered', 'Запуск management-команды из дашборда')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
        migrations.CreateModel(
            name='ManagementCommandRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('command_name', models.CharField(db_index=True, max_length=100, verbose_name='Команда')),
                ('args', models.JSONField(blank=True, default=dict, verbose_name='Аргументы')),
                ('status', models.CharField(choices=[('pending', 'В очереди'), ('running', 'Выполняется'), ('success', 'Успешно'), ('failed', 'Ошибка')], default='pending', max_length=10, verbose_name='Статус')),
                ('stdout', models.TextField(blank=True, verbose_name='Вывод')),
                ('stderr', models.TextField(blank=True, verbose_name='Ошибки')),
                ('triggered_by_username', models.CharField(blank=True, max_length=150, verbose_name='Логин (снимок)')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Поставлена')),
                ('started_at', models.DateTimeField(blank=True, null=True, verbose_name='Начата')),
                ('finished_at', models.DateTimeField(blank=True, null=True, verbose_name='Завершена')),
                ('celery_task_id', models.CharField(blank=True, max_length=255, verbose_name='ID celery-задачи')),
                ('triggered_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='management_command_runs', to=settings.AUTH_USER_MODEL, verbose_name='Кто запустил')),
            ],
            options={
                'verbose_name': 'Запуск management-команды',
                'verbose_name_plural': 'Запуски management-команд',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='managementcommandrun',
            index=models.Index(fields=['command_name', 'created_at'], name='cmd_run_name_time_idx'),
        ),
    ]
