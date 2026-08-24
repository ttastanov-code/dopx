# aggregates/migrations/0005_teamratingcorrection.py
"""
2026-08-24, продуктовое решение по итогам обсуждения: сигнал
stats_divergence (aggregates/tasks.py::detect_rating_stats_divergence_task)
раньше был чисто информационным — только создавал флаг в очереди модерации
и ничего не менял. Заводит TeamRatingCorrection — автоматическую,
ограниченную и самозатухающую поправку к TeamMatchAggregate.performance_score,
применяемую БЕЗ участия модератора, тем же принципом, что и остальные
структурные слои защиты (винзоризация, нейтральный якорь). См. докстринг
модели в aggregates/models.py.
"""
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0004_rename_aggregates__referee_9c3e5a_idx_aggregates__referee_6032b8_idx_and_more'),
        ('teams', '0003_team_kff_website_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeamRatingCorrection',
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
                    help_text='underrated_despite_dominance / overrated_despite_poor_play / пусто, если сейчас идёт затухание.',
                    max_length=40,
                    verbose_name='Последний обнаруженный паттерн',
                )),
                ('team', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='rating_correction', to='teams.team', verbose_name='Команда')),
            ],
            options={
                'verbose_name': 'Поправка рейтинга команды (авто)',
                'verbose_name_plural': 'Поправки рейтинга команд (авто)',
            },
        ),
    ]
