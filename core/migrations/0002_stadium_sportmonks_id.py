# core/migrations/0002_stadium_sportmonks_id.py
"""См. leagues/migrations/0003_league_sportmonks_id.py — тот же принцип.
Venue-данные у Sportmonks по КПЛ местами неполные (проверено вживую),
сверять глазами перед выводом на страницу матча."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='stadium',
            name='sportmonks_id',
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                unique=True,
            ),
        ),
    ]
