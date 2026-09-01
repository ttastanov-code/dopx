# notifications/migrations/0008_alter_notification_notification_type.py
# Ручная миграция (см. коммент в 0007) — добавляет 'match_event' в choices
# Notification.notification_type под push+in-app для live-событий матча
# (гол/автогол/пенальти/отменённый гол/красная карточка), см. докстринг
# NOTIFICATION_TYPES в notifications/models.py и notifications/tasks.py::
# notify_followers_match_event.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0007_alter_notification_notification_type'),
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
                ],
                default='system',
                max_length=30,
                verbose_name='Тип уведомления',
            ),
        ),
    ]
