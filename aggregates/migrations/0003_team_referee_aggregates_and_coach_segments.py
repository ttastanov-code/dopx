# aggregates/migrations/0003_team_referee_aggregates_and_coach_segments.py
"""
Anti-brigading, 2026-08-23 (продуктовый запрос "защита рейтингов от
сговора фан-базы"): заводит TeamMatchAggregate/RefereeMatchAggregate —
до этой миграции у команд и судей вообще не было персистентного агрегата
за матч (рейтинг считался live-Avg() на каждый рендер страницы, без
весов и без защиты, см. докстринги моделей в aggregates/models.py).
Заодно добавляет own_fans_avg/rival_fans_avg/neutral_avg на
CoachMatchAggregate — для игроков этот разрез уже существовал
(0002_playermatchaggregate_bias_segments), для тренеров не было.
"""
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0002_playermatchaggregate_bias_segments'),
        ('teams', '0003_team_kff_website_id'),
        ('referees', '0002_referee_photo'),
        ('matches', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='coachmatchaggregate',
            name='own_fans_avg',
            field=models.FloatField(
                blank=True,
                help_text='avg(average_score) от зрителей, поддержавших команду тренера',
                null=True,
                verbose_name='Средняя оценка от фанатов команды тренера',
            ),
        ),
        migrations.AddField(
            model_name='coachmatchaggregate',
            name='rival_fans_avg',
            field=models.FloatField(
                blank=True,
                help_text='avg(average_score) от зрителей, поддержавших команду-соперника',
                null=True,
                verbose_name='Средняя оценка от фанатов соперника',
            ),
        ),
        migrations.AddField(
            model_name='coachmatchaggregate',
            name='neutral_avg',
            field=models.FloatField(
                blank=True,
                help_text='avg(average_score) от зрителей без выбранной стороны/контекста',
                null=True,
                verbose_name='Средняя оценка от нейтральных зрителей',
            ),
        ),
        migrations.CreateModel(
            name='TeamMatchAggregate',
            fields=[
                ('id', models.UUIDField(editable=False, primary_key=True, serialize=False, default=uuid.uuid4)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('avg_tactics', models.FloatField(default=0.0, verbose_name='Средняя тактика')),
                ('avg_effort', models.FloatField(default=0.0, verbose_name='Средняя самоотдача')),
                ('avg_organization', models.FloatField(default=0.0, verbose_name='Средняя организация')),
                ('avg_mentality', models.FloatField(default=0.0, verbose_name='Средний менталитет')),
                ('total_votes', models.IntegerField(default=0, verbose_name='Всего голосов')),
                ('performance_score', models.FloatField(
                    default=0.0,
                    help_text='Взвешенное и винзоризованное среднее average_score (см. aggregates/services.py)',
                    verbose_name='Рейтинг команды',
                )),
                ('own_fans_avg', models.FloatField(
                    blank=True, null=True,
                    help_text='avg(average_score) от зрителей, поддержавших ЭТУ команду',
                    verbose_name='Средняя оценка от своих фанатов',
                )),
                ('rival_fans_avg', models.FloatField(
                    blank=True, null=True,
                    help_text='avg(average_score) от зрителей, поддержавших команду-соперника',
                    verbose_name='Средняя оценка от фанатов соперника',
                )),
                ('neutral_avg', models.FloatField(
                    blank=True, null=True,
                    help_text='avg(average_score) от зрителей без выбранной стороны/контекста',
                    verbose_name='Средняя оценка от нейтральных зрителей',
                )),
                ('match', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_aggregates', to='matches.match', verbose_name='Матч')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='match_aggregates', to='teams.team', verbose_name='Команда')),
            ],
            options={
                'verbose_name': 'Агрегат команды',
                'verbose_name_plural': 'Агрегаты команд',
                'ordering': ['-match__start_time'],
            },
        ),
        migrations.AddConstraint(
            model_name='teammatchaggregate',
            constraint=models.UniqueConstraint(fields=('team', 'match'), name='unique_team_match_aggregate'),
        ),
        migrations.AddIndex(
            model_name='teammatchaggregate',
            index=models.Index(fields=['team', 'match'], name='aggregates__team_id_9f6f4c_idx'),
        ),
        migrations.AddIndex(
            model_name='teammatchaggregate',
            index=models.Index(fields=['-performance_score'], name='aggregates__perform_5c1a2b_idx'),
        ),
        migrations.CreateModel(
            name='RefereeMatchAggregate',
            fields=[
                ('id', models.UUIDField(editable=False, primary_key=True, serialize=False, default=uuid.uuid4)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('avg_influence', models.FloatField(default=0.0, verbose_name='Среднее влияние на матч')),
                ('avg_decision_quality', models.FloatField(default=0.0, verbose_name='Среднее качество решений')),
                ('avg_fairness', models.FloatField(
                    default=0.0,
                    help_text='avg(MatchEvaluation.fairness) за этот матч — общий сигнал, не привязан к одному судье напрямую',
                    verbose_name='Средняя справедливость матча',
                )),
                ('total_votes', models.IntegerField(default=0, verbose_name='Всего голосов')),
                ('performance_score', models.FloatField(
                    default=0.0,
                    help_text='0.6*decision_quality + 0.3*fairness + 0.1*(10 - influence/10) — см. season_squad/services.py::_build_referee_pool (перенесённая формула)',
                    verbose_name='Рейтинг судейства',
                )),
                ('home_fans_avg', models.FloatField(
                    blank=True, null=True,
                    help_text='avg(decision_quality) от зрителей, поддержавших домашнюю команду',
                    verbose_name='Средняя оценка от фанатов домашней команды',
                )),
                ('away_fans_avg', models.FloatField(
                    blank=True, null=True,
                    help_text='avg(decision_quality) от зрителей, поддержавших гостевую команду',
                    verbose_name='Средняя оценка от фанатов гостевой команды',
                )),
                ('neutral_avg', models.FloatField(
                    blank=True, null=True,
                    help_text='avg(decision_quality) от зрителей без выбранной стороны/контекста',
                    verbose_name='Средняя оценка от нейтральных зрителей',
                )),
                ('match', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='referee_aggregates', to='matches.match', verbose_name='Матч')),
                ('referee', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='match_aggregates', to='referees.referee', verbose_name='Судья')),
            ],
            options={
                'verbose_name': 'Агрегат судейства',
                'verbose_name_plural': 'Агрегаты судейства',
                'ordering': ['-match__start_time'],
            },
        ),
        migrations.AddConstraint(
            model_name='refereematchaggregate',
            constraint=models.UniqueConstraint(fields=('referee', 'match'), name='unique_referee_match_aggregate'),
        ),
        migrations.AddIndex(
            model_name='refereematchaggregate',
            index=models.Index(fields=['referee', 'match'], name='aggregates__referee_9c3e5a_idx'),
        ),
        migrations.AddIndex(
            model_name='refereematchaggregate',
            index=models.Index(fields=['-performance_score'], name='aggregates__perform_a1d3c2_idx'),
        ),
    ]
