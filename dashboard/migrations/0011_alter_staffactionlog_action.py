# dashboard/migrations/0011_alter_staffactionlog_action.py
# Ручная миграция (см. докстринг 0009 — тот же принцип: choices не
# enforced на уровне БД, эта миграция только приводит state в актуальное
# состояние). Синхронизирует накопившиеся невыгруженные пачки choices
# разом: duplicate_players_merged/duplicate_player_flag_dismissed (2026-09-22,
# очередь «Дубли игроков» — по какой-то причине осталась без своей
# миграции), match_manual_edit/match_recalc_triggered (2026-09-23, раздел
# «Матчи»), platform_setting_created/changed/deleted (2026-09-23, раздел
# «Настройки платформы») и user_banned/user_unbanned/user_trust_score_reset
# (2026-09-23, раздел «Пользователи») — см. dashboard/models.py::AuditAction.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0010_alter_managementcommandrun_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='action',
            field=models.CharField(choices=[('antifraud_flag_confirmed', 'Флаг подтверждён'), ('antifraud_flag_dismissed', 'Флаг отклонён'), ('match_resync', 'Ручной ресинк матча'), ('celery_task_triggered', 'Запуск celery-задачи вручную'), ('celery_task_revoked', 'Отзыв/остановка celery-задачи'), ('sportmonks_health_check', 'Проверка доступности Sportmonks API'), ('system_announcement_sent', 'Отправлено системное объявление'), ('data_error_report_resolved', 'Жалоба на данные матча закрыта'), ('parser_discrepancy_reviewed', 'Расхождение импорта разобрано'), ('management_command_triggered', 'Запуск management-команды из дашборда'), ('name_suggestion_approved', 'Предложение ИИ по ФИО подтверждено'), ('name_suggestion_rejected', 'Предложение ИИ по ФИО отклонено'), ('duplicate_players_merged', 'Дубли игроков объединены'), ('duplicate_player_flag_dismissed', 'Флаг дубля игрока отклонён (разные люди)'), ('match_manual_edit', 'Матч отредактирован вручную'), ('match_recalc_triggered', 'Ручной пересчёт агрегатов матча'), ('platform_setting_created', 'Создана настройка платформы'), ('platform_setting_changed', 'Изменена настройка платформы'), ('platform_setting_deleted', 'Удалена настройка платформы'), ('user_banned', 'Пользователь заблокирован'), ('user_unbanned', 'Пользователь разблокирован'), ('user_trust_score_reset', 'Оценка доверия пользователя сброшена')], db_index=True, max_length=50, verbose_name='Действие'),
        ),
    ]
