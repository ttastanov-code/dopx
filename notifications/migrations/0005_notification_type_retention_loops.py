# Ручная миграция (нет доступа к makemigrations в песочнице разработки —
# см. коммент в notifications/migrations/0003_notification_email_sent_at_and_more.py).
# Расширяет choices Notification.notification_type тремя новыми типами для
# retention loops (2026-08-21): prediction_closing (loop 1), weekly_digest
# (loop 2), prediction_result (loop 3). State-only — колонка не меняется.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0004_contactsubmission_dispute_category'),
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
                ],
                max_length=30,
                verbose_name='Тип уведомления',
            ),
        ),
    ]
