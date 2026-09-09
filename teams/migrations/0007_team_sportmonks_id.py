# teams/migrations/0007_team_sportmonks_id.py
"""См. leagues/migrations/0003_league_sportmonks_id.py — тот же принцип.
Для команд заполняется вручную по итогам сверки 16 клубов КПЛ
(docs/sportmonks-migration-plan.md, фаза 2)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0006_remove_team_colors'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
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
