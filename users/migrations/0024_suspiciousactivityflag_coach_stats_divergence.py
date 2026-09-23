# users/migrations/0024_suspiciousactivityflag_coach_stats_divergence.py
"""
2026-09-24: источник "coach_stats_divergence" — оценки тренера расходятся с
тем, как объективно играла его команда (aggregates/tasks.py::
detect_coach_rating_stats_divergence_task). Только choices — схема БД не
меняется.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0023_alter_userbadge_is_stale'),
    ]

    operations = [
        migrations.AlterField(
            model_name='suspiciousactivityflag',
            name='source',
            field=models.CharField(
                choices=[
                    ('fast_wizard', 'Слишком быстрое заполнение вайзарда оценки'),
                    ('ip_cluster', 'Кластер аккаунтов с одного IP'),
                    ('extreme_bias', 'Экстремальная историческая предвзятость'),
                    ('vote_spike', 'Аномальный всплеск голосования (возможный сговор)'),
                    ('stats_divergence', 'Рейтинг команды расходится с объективной статистикой матча'),
                    ('player_stats_divergence', 'Рейтинг игрока расходится с объективной статистикой матча'),
                    ('coach_stats_divergence', 'Оценки тренера расходятся с игрой его команды'),
                    ('manual', 'Отмечено вручную модератором'),
                ],
                max_length=30,
                verbose_name='Источник сигнала',
            ),
        ),
    ]
