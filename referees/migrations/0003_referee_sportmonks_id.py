# referees/migrations/0003_referee_sportmonks_id.py
"""См. leagues/migrations/0003_league_sportmonks_id.py — тот же принцип.
У Referee особенно важно: KFF отдавал судью свободным текстом без id
(parsers/kff/importers.py::get_or_create_referee_by_name), Sportmonks —
стабильной сущностью со своим id с самого начала."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('referees', '0002_referee_photo'),
    ]

    operations = [
        migrations.AddField(
            model_name='referee',
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
