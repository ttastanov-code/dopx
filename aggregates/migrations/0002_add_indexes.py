# aggregates/migrations/0002_add_indexes.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='playermatchaggregate',
            index=models.Index(fields=['player', 'match', 'performance_score'], name='aggregates_player__22222_idx'),
        ),
        migrations.AddIndex(
            model_name='playermatchaggregate',
            index=models.Index(fields=['match', 'performance_score'], name='aggregates_match_p_33333_idx'),
        ),
    ]