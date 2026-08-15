import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0009_follow'),
    ]

    operations = [
        migrations.CreateModel(
            name='PushSubscription',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('endpoint', models.URLField(max_length=500, unique=True, verbose_name='Endpoint')),
                ('p256dh', models.CharField(max_length=255, verbose_name='Ключ p256dh')),
                ('auth', models.CharField(max_length=255, verbose_name='Ключ auth')),
                ('user_agent', models.CharField(blank=True, max_length=255, verbose_name='User-Agent')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='push_subscriptions', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Push-подписка',
                'verbose_name_plural': 'Push-подписки',
            },
        ),
        migrations.AddIndex(
            model_name='pushsubscription',
            index=models.Index(fields=['user'], name='push_subscription_user_idx'),
        ),
    ]
