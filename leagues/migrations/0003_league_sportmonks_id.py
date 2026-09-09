# leagues/migrations/0003_league_sportmonks_id.py
"""
Переход с KFF на Sportmonks (docs/sportmonks-migration-plan.md) —
отдельное поле под id из нового источника, existing external_id (KFF)
не трогаем. Написана вручную по образцу matches/migrations/0008_...
(makemigrations недоступен в песочнице разработки, см. docs/BACKLOG.md).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('leagues', '0002_league_is_primary'),
    ]

    operations = [
        migrations.AddField(
            model_name='league',
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
