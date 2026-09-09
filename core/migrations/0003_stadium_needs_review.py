# core/migrations/0003_stadium_needs_review.py
"""2026-09-09: см. core/models_stadium.py::Stadium.needs_review докстринг —
защита от неправильных venue-данных Sportmonks у дженерично названных
стадионов (найдено вживую пользователем: "Центральный стадион" во многих
матчах подменяется на "Центральный стадион Хисора", Таджикистан)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_stadium_sportmonks_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='stadium',
            name='needs_review',
            field=models.BooleanField(default=True),
        ),
    ]
