# aggregates/migrations/0008_aggregate_rating_correction_applied.py
# Ручная миграция (Django недоступен в песочнице для makemigrations).
# 2026-09-23: PlayerMatchAggregate/TeamMatchAggregate.rating_correction_applied —
# сколько авто-поправки вшито в performance_score конкретного матча. Детектор
# расхождения (aggregates/tasks.py::_check_*_stats_divergence) вычитает её,
# чтобы сравнивать чистую оценку болельщиков, а не оценку + собственную
# прошлую поправку (петля обратной связи, из-за которой поправка игрока
# могла сама по себе менять знак: −0.23 → +0.25).
# Существующие строки получают 0.0 — для уже посчитанных матчей точная
# величина вшитой поправки неизвестна (история не хранилась).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0007_playerratingcorrection'),
    ]

    operations = [
        migrations.AddField(
            model_name='playermatchaggregate',
            name='rating_correction_applied',
            field=models.FloatField(default=0.0, verbose_name='Вшитая авто-поправка'),
        ),
        migrations.AddField(
            model_name='teammatchaggregate',
            name='rating_correction_applied',
            field=models.FloatField(default=0.0, verbose_name='Вшитая авто-поправка'),
        ),
    ]
