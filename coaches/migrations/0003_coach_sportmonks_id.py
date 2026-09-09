# coaches/migrations/0003_coach_sportmonks_id.py
"""См. leagues/migrations/0003_league_sportmonks_id.py — тот же принцип."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coaches', '0002_coach_photo'),
    ]

    operations = [
        migrations.AddField(
            model_name='coach',
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
