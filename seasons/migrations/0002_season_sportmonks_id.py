# seasons/migrations/0002_season_sportmonks_id.py
"""См. leagues/migrations/0003_league_sportmonks_id.py — тот же принцип."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('seasons', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='season',
            name='sportmonks_id',
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                unique=True,
                verbose_name='Sportmonks ID',
            ),
        ),
    ]
