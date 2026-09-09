# users/migrations/0020_suspiciousactivityflag_player_stats_divergence.py
"""
2026-09-08, по прямой просьбе пользователя ("статистику матча... против
накрутки и неадекватной оценки"): добавляет источник "player_stats_divergence"
в SuspiciousActivityFlag.SOURCE_CHOICES — новая задача aggregates/tasks.py::
detect_player_rating_stats_divergence_task, аналог существующего
"stats_divergence" (0015), но на уровне игрока (aggregates.models.
PlayerRatingCorrection). Заодно поправлена формулировка "stats_divergence" —
была "...с объективной статистикой KFF", источник этих данных сменился на
Sportmonks (docs/sportmonks-migration-plan.md), текст больше не точен.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0019_new_achievements_and_legendary_tier'),
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
                    ('manual', 'Отмечено вручную модератором'),
                ],
                max_length=30,
                verbose_name='Источник сигнала',
            ),
        ),
    ]
