# players/migrations/0005_player_sportmonks_id_playersidelined.py
"""
Переход с KFF на Sportmonks (docs/sportmonks-migration-plan.md):
1. sportmonks_id на Player — см. leagues/migrations/0003_league_sportmonks_id.py,
   тот же принцип, заполняется скриптом реконсиляции (фаза 2 плана).
2. PlayerSidelined — новая модель под дисквалификации/травмы игроков,
   у KFF аналога не было, источник — endpoint sidelined у Sportmonks
   (проверено вживую на реальных данных КПЛ).

Написана вручную (makemigrations недоступен в песочнице разработки, см.
docs/BACKLOG.md), по образцу events/migrations/0003_eventreaction.py.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0004_player_roster_absence_streak'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='sportmonks_id',
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                unique=True,
                verbose_name='Sportmonks ID',
            ),
        ),
        migrations.CreateModel(
            name='PlayerSidelined',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('category', models.CharField(
                    choices=[('injury', 'Травма'), ('suspended', 'Дисквалификация'), ('other', 'Другое')],
                    default='other',
                    max_length=20,
                    verbose_name='Категория',
                )),
                ('start_date', models.DateField(blank=True, null=True, verbose_name='С')),
                ('end_date', models.DateField(blank=True, null=True, verbose_name='По')),
                ('sportmonks_id', models.CharField(blank=True, max_length=100, null=True, unique=True, verbose_name='Sportmonks ID')),
                ('player', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='sidelined_periods',
                    to='players.player',
                    verbose_name='Игрок',
                )),
            ],
            options={
                'verbose_name': 'Недоступность игрока',
                'verbose_name_plural': 'Недоступность игроков',
                'ordering': ['-start_date'],
            },
        ),
        migrations.AddIndex(
            model_name='playersidelined',
            index=models.Index(fields=['player', 'end_date'], name='players_pla_player__f7d1d1_idx'),
        ),
    ]
