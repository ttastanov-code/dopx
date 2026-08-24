# users/migrations/0015_suspiciousactivityflag_stats_divergence_source.py
"""
Anti-brigading, независимый внешний сигнал (2026-08-23): добавляет источник
"stats_divergence" в SuspiciousActivityFlag.SOURCE_CHOICES — новая задача
aggregates/tasks.py::detect_rating_stats_divergence_task сравнивает рейтинг
сообщества с объективной статистикой матча от KFF (matches.models.
MatchTeamStatistics), а не с самими голосами — см. докстринг модели.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0014_antifraudthreshold'),
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
                    ('stats_divergence', 'Рейтинг сообщества расходится с объективной статистикой KFF'),
                    ('manual', 'Отмечено вручную модератором'),
                ],
                max_length=30,
                verbose_name='Источник сигнала',
            ),
        ),
    ]
