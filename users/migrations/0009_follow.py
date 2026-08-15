import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0001_initial'),
        ('teams', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('users', '0008_user_is_profile_public'),
    ]

    operations = [
        migrations.CreateModel(
            name='Follow',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('player', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='followers', to='players.player', verbose_name='Игрок')),
                ('team', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='followers', to='teams.team', verbose_name='Команда')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='follows', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Подписка',
                'verbose_name_plural': 'Подписки',
            },
        ),
        migrations.AddIndex(
            model_name='follow',
            index=models.Index(fields=['user'], name='follow_user_idx'),
        ),
        migrations.AddConstraint(
            model_name='follow',
            constraint=models.UniqueConstraint(condition=models.Q(('player__isnull', False)), fields=('user', 'player'), name='unique_follow_player'),
        ),
        migrations.AddConstraint(
            model_name='follow',
            constraint=models.UniqueConstraint(condition=models.Q(('team__isnull', False)), fields=('user', 'team'), name='unique_follow_team'),
        ),
        migrations.AddConstraint(
            model_name='follow',
            constraint=models.CheckConstraint(condition=models.Q(('player__isnull', False), ('team__isnull', True)) | models.Q(('player__isnull', True), ('team__isnull', False)), name='follow_exactly_one_target'),
        ),
    ]
