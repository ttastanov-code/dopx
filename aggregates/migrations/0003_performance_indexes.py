# aggregates/migrations/0003_performance_indexes.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0002_add_indexes'),
    ]

    operations = [
        # Индексы для PlayerMatchAggregate
        migrations.AddIndex(
            model_name='playermatchaggregate',
            index=models.Index(
                fields=['-performance_score'],
                name='agg_player_perf_idx'
            ),
        ),
        migrations.AddIndex(
            model_name='playermatchaggregate',
            index=models.Index(
                fields=['match', '-performance_score'],
                name='agg_match_perf_idx'
            ),
        ),
        # Индекс для MatchAggregate (только match, без match__start_time)
        migrations.AddIndex(
            model_name='matchaggregate',
            index=models.Index(
                fields=['match'],
                name='agg_match_idx'
            ),
        ),
    ]