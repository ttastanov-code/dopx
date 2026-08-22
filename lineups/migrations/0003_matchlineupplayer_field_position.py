# lineups/migrations/0003_matchlineupplayer_field_position.py
"""Добавляет MatchLineupPlayer.field_position — сырое значение поля
'field_position' из ответа KFF /games/<id>/lineup (C/L/R/LC/RC), которое
раньше не импортировалось вообще (см. докстринг на самом поле в
lineups/models.py и players/positions.py про причину). Отдельно от
MatchLineupPlayer.position (амплуа GK/D/DM/M/AM/F) — комбинация двух полей
даёт точный слот формации (например position='D' + field_position='L' =
левый защитник), тогда как одно 'position' даёт только общую зону."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("lineups", "0002_normalize_position_casing"),
    ]

    operations = [
        migrations.AddField(
            model_name="matchlineupplayer",
            name="field_position",
            field=models.CharField(
                blank=True, max_length=5, verbose_name="Сторона на поле"
            ),
        ),
    ]
