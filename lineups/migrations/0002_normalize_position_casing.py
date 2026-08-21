# lineups/migrations/0002_normalize_position_casing.py
"""Тот же бэкафилл, что players/migrations/0002, но для позиции игрока В
КОНКРЕТНОМ МАТЧЕ (MatchLineupPlayer.position) — отдельное поле, та же
проблема с разнобоем регистра из KFF."""
from django.db import migrations


def normalize_positions(apps, schema_editor):
    MatchLineupPlayer = apps.get_model("lineups", "MatchLineupPlayer")
    for lp in MatchLineupPlayer.objects.exclude(position="").exclude(position__isnull=True):
        cleaned = lp.position.strip().upper()
        if cleaned != lp.position:
            MatchLineupPlayer.objects.filter(pk=lp.pk).update(position=cleaned)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("lineups", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(normalize_positions, noop_reverse),
    ]
