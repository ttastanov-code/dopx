# Generated manually — в песочнице разработки нет сетевого доступа к
# PyPI, поэтому `manage.py makemigrations` тут не запустить. Файл написан
# вручную по образцу существующих миграций проекта (см. например
# users/migrations/0006_...); поля/индексы 1:1 совпадают с
# analytics/models.py. Перед мёржем в основную ветку рекомендуется
# прогнать `python manage.py makemigrations --check` на машине с рабочим
# окружением — если Django сгенерирует ту же схему без diff'а, эта
# миграция подтверждена как корректная.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AnalyticsEvent',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='Создано')),
                ('event_name', models.CharField(choices=[
                    ('page_view', 'Просмотр страницы'),
                    ('user_registered', 'Регистрация'),
                    ('user_login', 'Вход'),
                    ('wizard_started', 'Начало оценки матча'),
                    ('wizard_step_completed', 'Завершён шаг оценки'),
                    ('wizard_abandoned', 'Оценка брошена'),
                    ('evaluation_completed', 'Оценка матча завершена'),
                    ('share_card_viewed', "Просмотр шер-карточки"),
                    ('share_clicked', "Клик 'Поделиться'"),
                    ('profile_viewed', 'Просмотр публичного профиля'),
                    ('leaderboard_viewed', 'Просмотр лидерборда'),
                ], db_index=True, max_length=50, verbose_name='Событие')),
                ('anonymous_id', models.UUIDField(blank=True, db_index=True, null=True, verbose_name='Анонимный ID')),
                ('session_id', models.CharField(blank=True, max_length=40)),
                ('properties', models.JSONField(blank=True, default=dict, verbose_name='Свойства')),
                ('url_path', models.CharField(blank=True, max_length=500)),
                ('referrer', models.CharField(blank=True, max_length=500)),
                ('utm_source', models.CharField(blank=True, max_length=100)),
                ('utm_medium', models.CharField(blank=True, max_length=100)),
                ('utm_campaign', models.CharField(blank=True, max_length=100)),
                ('ip_hash', models.CharField(blank=True, help_text='SHA-256(IP+SECRET_KEY) — сырой IP никогда не пишем, см. analytics.services.hash_ip', max_length=64)),
                ('user_agent', models.CharField(blank=True, max_length=300)),
                ('user', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='analytics_events',
                    to=settings.AUTH_USER_MODEL,
                    help_text='SET_NULL: агрегаты должны переживать удаление аккаунта',
                    verbose_name='Пользователь',
                )),
            ],
            options={
                'verbose_name': 'Событие аналитики',
                'verbose_name_plural': 'События аналитики',
            },
        ),
        migrations.AddIndex(
            model_name='analyticsevent',
            index=models.Index(fields=['event_name', 'created_at'], name='analytics_event_created_idx'),
        ),
        migrations.AddIndex(
            model_name='analyticsevent',
            index=models.Index(fields=['user', 'created_at'], name='analytics_user_created_idx'),
        ),
        migrations.AddIndex(
            model_name='analyticsevent',
            index=models.Index(fields=['anonymous_id', 'created_at'], name='analytics_anon_created_idx'),
        ),
    ]
