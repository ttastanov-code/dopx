# matches/migrations/0010_match_sportmonks_id_referee_crew.py
"""
Переход с KFF на Sportmonks (docs/sportmonks-migration-plan.md):
1. sportmonks_id — см. leagues/migrations/0003_league_sportmonks_id.py,
   тот же принцип, existing external_id (KFF) не трогаем.
2. referee_crew — Sportmonks отдаёт полную бригаду судей на матч (главный
   + 2 ассистента + четвёртый судья), проверено вживую: 25/25 матчей КПЛ
   с полной бригадой. FK referee (Match.referee, существующее поле) хранит
   только главного судью — под остальных заводить отдельную M2M-модель
   избыточно, простой JSON с готовыми именами дешевле.

Написана вручную (makemigrations недоступен в песочнице разработки, см.
docs/BACKLOG.md).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0009_match_was_rescheduled'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='sportmonks_id',
            field=models.CharField(
                blank=True,
                max_length=100,
                null=True,
                unique=True,
                verbose_name='Sportmonks ID',
            ),
        ),
        migrations.AddField(
            model_name='match',
            name='referee_crew',
            field=models.JSONField(
                blank=True,
                null=True,
                verbose_name='Бригада судей (ассистенты, 4-й судья)',
            ),
        ),
    ]
