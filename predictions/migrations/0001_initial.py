import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('matches', '0005_match_tour'),
    ]

    operations = [
        migrations.CreateModel(
            name='MatchPrediction',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('choice', models.CharField(choices=[('1', 'П1 — победа хозяев'), ('X', 'X — ничья'), ('2', 'П2 — победа гостей')], max_length=1, verbose_name='Прогноз')),
                ('match', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='predictions', to='matches.match', verbose_name='Матч')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='match_predictions', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
            ],
            options={
                'verbose_name': 'Прогноз на матч',
                'verbose_name_plural': 'Прогнозы на матчи',
            },
        ),
        migrations.AddIndex(
            model_name='matchprediction',
            index=models.Index(fields=['match', 'choice'], name='match_prediction_choice_idx'),
        ),
        migrations.AddConstraint(
            model_name='matchprediction',
            constraint=models.UniqueConstraint(fields=('match', 'user'), name='unique_match_prediction'),
        ),
    ]
