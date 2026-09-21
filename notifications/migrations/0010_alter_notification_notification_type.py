# notifications/migrations/0010_alter_notification_notification_type.py
# Ручная миграция (см. коммент в 0007/0008) — добавляет 'match_started' и
# 'lineups_available' в choices Notification.notification_type под новые
# пуши "матч начался"/"составы объявлены" (аудит пуш-системы 2026-09-21,
# см. NOTIFICATION_TYPES в notifications/models.py и notifications/tasks.py::
# notify_followers_match_started/notify_followers_lineups_available).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0009_contactsubmission_data_error_category'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notification',
            name='notification_type',
            field=models.CharField(
                choices=[
                    ('welcome', 'Приветственное письмо'),
                    ('match_finished', 'Матч завершён / Голосование открыто'),
                    ('voting_open', 'Голосование открыто'),
                    ('voting_closing', 'Напоминание о закрытии голосования'),
                    ('new_badge', 'Новое достижение'),
                    ('level_up', 'Повышение уровня'),
                    ('aggregate_updated', 'Обновление рейтинга'),
                    ('top_performance', 'Топ-выступление'),
                    ('verification_required', 'Требуется подтверждение email'),
                    ('system', 'Системное уведомление'),
                    ('prediction_closing', 'Скоро закроется приём прогнозов'),
                    ('weekly_digest', 'Персональная сводка недели'),
                    ('prediction_result', 'Прогноз vs результат матча'),
                    ('round_results', 'Итоги «DOPX Лучшие тура»'),
                    ('match_event', 'Live-событие матча'),
                    ('match_started', 'Матч начался'),
                    ('lineups_available', 'Составы объявлены'),
                ],
                default='system',
                max_length=30,
                verbose_name='Тип уведомления',
            ),
        ),
    ]
