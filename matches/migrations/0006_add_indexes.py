# matches/migrations/0006_add_indexes.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0005_match_has_lineup'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='match',
            index=models.Index(fields=['league', 'season', 'start_time'], name='matches_match_league_sea_12345_idx'),
        ),
        migrations.AddIndex(
            model_name='match',
            index=models.Index(fields=['status', 'start_time'], name='matches_match_status__67890_idx'),
        ),
    ]