# matches/migrations/0012_matchteamstatistics_dangerous_attacks.py
# Новое поле dangerous_attacks на MatchTeamStatistics (2026-09-09, аудит
# неиспользуемых полей Sportmonks) — см. комментарий в matches/models.py и
# parsers/sportmonks/importers.py::TEAM_STAT_DEV_NAME_MAP.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0011_remove_match_stadium'),
    ]

    operations = [
        migrations.AddField(
            model_name='matchteamstatistics',
            name='dangerous_attacks',
            field=models.IntegerField(blank=True, null=True, verbose_name='Опасные атаки'),
        ),
    ]
