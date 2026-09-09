# aggregates/migrations/0007_playerratingcorrection.py
"""
2026-09-08, по прямой просьбе пользователя ("статистику матча... для
рекомендации быстрой оценки... против накрутки"): аналог TeamRatingCorrection
(миграции 0005/0006) на уровне игрока. См. докстринг PlayerRatingCorrection
в aggregates/models.py и detect_player_rating_stats_divergence_task в
aggregates/tasks.py.
"""
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0006_teamratingcorrection_suppressed_until'),
        ('players', '0006_rename_playersidelined_index'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlayerRatingCorrection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('correction', models.FloatField(
                    default=0.0,
                    help_text='Прибавляется к performance_score на каждом пересчёте, ограничена и самозатухает.',
                    verbose_name='Текущая поправка',
                )),
                ('last_pattern', models.CharField(
                    blank=True,
                    help_text='underrated_despite_stats / overrated_despite_stats / пусто, если сейчас идёт затухание.',
                    max_length=40,
                    verbose_name='Последний обнаруженный паттерн',
                )),
                ('suppressed_until', models.DateTimeField(
                    blank=True, null=True,
                    help_text=(
                        'Тот же приём, что у TeamRatingCorrection.suppressed_until — пока это поле в будущем, '
                        '_check_player_stats_divergence пропускает игрока, не трогая поправку (модератор явно '
                        'отклонил флаг как объяснимый).'
                    ),
                    verbose_name='Подавлено до',
                )),
                ('player', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='rating_correction', to='players.player', verbose_name='Игрок',
                )),
            ],
            options={
                'verbose_name': 'Поправка рейтинга игрока (авто)',
                'verbose_name_plural': 'Поправки рейтинга игроков (авто)',
            },
        ),
    ]
