# dashboard/migrations/0012_alter_staffactionlog_action.py
# Ручная миграция (см. докстринг 0009/0011 — choices не enforced на уровне
# БД, эта миграция только приводит state в актуальное состояние). Добавляет
# evaluation_session_deleted (2026-09-23, раздел «Модерация оценок») — см.
# dashboard/models.py::AuditAction, dashboard/views.py::evaluation_session_delete.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0011_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление'), ('data_error_report_resolved', 'Жалоба на данные матча закрыта'), ('parser_discrepancy_reviewed', 'Расхождение импорта разобрано'), ('management_command_triggered', 'Запуск management-команды из дашборда'), ('name_suggestion_approved', 'Предложение ИИ по ФИО подтверждено'), ('name_suggestion_rejected', 'Предложение ИИ по ФИО отклонено'), ('duplicate_players_merged', 'Дубли игроков объединены'), ('duplicate_player_flag_dismissed', 'Флаг дубля игрока отклонён (разные люди)'), ('match_manual_edit', 'Матч отредактирован вручную'), ('match_recalc_triggered', 'Ручной пересчёт агрегатов матча'), ('platform_setting_created', 'Создана настройка платформы'), ('platform_setting_changed', 'Изменена настройка платформы'), ('platform_setting_deleted', 'Удалена настройка платформы'), ('user_banned', 'Пользователь заблокирован'), ('user_unbanned', 'Пользователь разблокирован'), ('user_trust_score_reset', 'Оценка доверия пользователя сброшена'), ('evaluation_session_deleted', 'Сессия оценки удалена (модерация)')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
