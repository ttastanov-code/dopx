# matches/migrations/0006_matchteamstatistics_matchplayerstatistics.py
"""
Anti-brigading, независимый внешний сигнал (2026-08-23, продуктовый запрос
"можем использовать статистику на KFF?"): заводит MatchTeamStatistics и
MatchPlayerStatistics — объективные факты матча (удары/владение/карточки
и т.д.) с GET /api/v1/games/{id}/stats, НЕ зависящие от голосов
пользователей DOPX. Используется как проверяемый внешний ориентир в
aggregates/tasks.py::detect_rating_stats_divergence_task — см. докстринги
моделей в matches/models.py.
"""
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0005_match_tour'),
        ('teams', '0003_team_kff_website_id'),
        ('players', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='MatchTeamStatistics',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('possession_percent', models.FloatField(blank=True, null=True, verbose_name='Владение мячом, %')),
                ('shots', models.IntegerField(blank=True, null=True, verbose_name='Удары')),
                ('shots_on_goal', models.IntegerField(blank=True, null=True, verbose_name='Удары в створ')),
                ('shots_on_bar', models.IntegerField(blank=True, null=True, verbose_name='Удары в штангу')),
                ('shots_blocked', models.IntegerField(blank=True, null=True, verbose_name='Удары заблокированы')),
                ('corners', models.IntegerField(blank=True, null=True, verbose_name='Угловые')),
                ('offsides', models.IntegerField(blank=True, null=True, verbose_name='Офсайды')),
                ('fouls', models.IntegerField(blank=True, null=True, verbose_name='Фолы')),
                ('yellow_cards', models.IntegerField(blank=True, null=True, verbose_name='Жёлтые карточки')),
                ('red_cards', models.IntegerField(blank=True, null=True, verbose_name='Красные карточки')),
                ('penalties', models.IntegerField(blank=True, null=True, verbose_name='Пенальти')),
                ('saves', models.IntegerField(blank=True, null=True, verbose_name='Сейвы')),
                ('xg', models.FloatField(blank=True, null=True, verbose_name='Ожидаемые голы (xG)')),
                ('passes', models.IntegerField(blank=True, null=True, verbose_name='Передачи')),
                ('pass_accuracy', models.FloatField(blank=True, null=True, verbose_name='Точность передач, %')),
                ('key_passes', models.IntegerField(blank=True, null=True, verbose_name='Ключевые передачи')),
                ('crosses', models.IntegerField(blank=True, null=True, verbose_name='Кроссы')),
                ('raw', models.JSONField(blank=True, default=dict, verbose_name='Сырые данные из API')),
                ('match', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='team_statistics', to='matches.match', verbose_name='Матч')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='match_statistics', to='teams.team', verbose_name='Команда')),
            ],
            options={
                'verbose_name': 'Статистика команды за матч',
                'verbose_name_plural': 'Статистика команд за матч',
            },
        ),
        migrations.AddConstraint(
            model_name='matchteamstatistics',
            constraint=models.UniqueConstraint(fields=('match', 'team'), name='unique_match_team_statistics'),
        ),
        migrations.CreateModel(
            name='MatchPlayerStatistics',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('fouls', models.IntegerField(blank=True, null=True, verbose_name='Фолы')),
                ('saves', models.IntegerField(blank=True, null=True, verbose_name='Сейвы')),
                ('shots', models.IntegerField(blank=True, null=True, verbose_name='Удары')),
                ('shots_on_target', models.IntegerField(blank=True, null=True, verbose_name='Удары в створ')),
                ('shots_missed', models.IntegerField(blank=True, null=True, verbose_name='Удары мимо')),
                ('shots_on_bar', models.IntegerField(blank=True, null=True, verbose_name='Удары в штангу')),
                ('shots_blocked', models.IntegerField(blank=True, null=True, verbose_name='Удары заблокированы')),
                ('corners', models.IntegerField(blank=True, null=True, verbose_name='Угловые')),
                ('offsides', models.IntegerField(blank=True, null=True, verbose_name='Офсайды')),
                ('penalties', models.IntegerField(blank=True, null=True, verbose_name='Пенальти')),
                ('missed_penalty', models.IntegerField(blank=True, null=True, verbose_name='Незабитые пенальти')),
                ('possessions', models.IntegerField(blank=True, null=True, verbose_name='Владения мячом')),
                ('raw', models.JSONField(blank=True, default=dict, verbose_name='Сырые данные из API')),
                ('match', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='player_statistics', to='matches.match', verbose_name='Матч')),
                ('player', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='match_statistics', to='players.player', verbose_name='Игрок')),
                ('team', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='player_match_statistics', to='teams.team', verbose_name='Команда')),
            ],
            options={
                'verbose_name': 'Статистика игрока за матч',
                'verbose_name_plural': 'Статистика игроков за матч',
            },
        ),
        migrations.AddConstraint(
            model_name='matchplayerstatistics',
            constraint=models.UniqueConstraint(fields=('match', 'player'), name='unique_match_player_statistics'),
        ),
        migrations.AddIndex(
            model_name='matchplayerstatistics',
            index=models.Index(fields=['team', 'match'], name='matches_mat_team_id_5f2e91_idx'),
        ),
    ]
