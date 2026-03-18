# evaluations/migrations/0002_add_indexes.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0001_initial'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='playerevaluation',
            index=models.Index(fields=['match', 'player', 'user'], name='evaluations_match_pla_12345_idx'),
        ),
        migrations.AddIndex(
            model_name='playerevaluation',
            index=models.Index(fields=['user', 'match'], name='evaluations_user_ma_67890_idx'),
        ),
        migrations.AddIndex(
            model_name='matchevaluation',
            index=models.Index(fields=['match', 'user'], name='evaluations_match_us_11111_idx'),
        ),
    ]