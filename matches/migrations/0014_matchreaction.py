# matches/migrations/0014_matchreaction.py
# Новая модель Match.MatchReaction (2026-09-10, редизайн карточки матча,
# пункт 11 брифа — "Реакция сообщества": Матч тура / Неожиданный результат /
# Скучный матч). См. докстринг модели в matches/models.py.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('matches', '0013_match_decided_administratively'),
    ]

    operations = [
        migrations.CreateModel(
            name='MatchReaction',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('reaction', models.CharField(
                    choices=[
                        ('match_of_round', 'Матч тура'),
                        ('upset', 'Неожиданный результат'),
                        ('boring', 'Скучный матч'),
                    ],
                    max_length=20, verbose_name='Реакция',
                )),
                ('match', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='reactions',
                    to='matches.match', verbose_name='Матч',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='match_reactions',
                    to=settings.AUTH_USER_MODEL, verbose_name='Пользователь',
                )),
            ],
            options={
                'verbose_name': 'Реакция на матч',
                'verbose_name_plural': 'Реакции на матчи',
            },
        ),
        migrations.AddIndex(
            model_name='matchreaction',
            index=models.Index(fields=['match', 'reaction'], name='match_reaction_type_idx'),
        ),
        migrations.AddConstraint(
            model_name='matchreaction',
            constraint=models.UniqueConstraint(fields=('match', 'user'), name='unique_match_reaction'),
        ),
    ]
