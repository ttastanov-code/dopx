# Одно событие поставщика — одна запись в матче.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("events", "0007_merge_duplicate_events")]

    operations = [
        migrations.AddConstraint(
            model_name="matchevent",
            constraint=models.UniqueConstraint(fields=("match", "sportmonks_id"), name="unique_match_event_sportmonks_id"),
        ),
    ]
