# matches/migrations/0005_match_tour.py
"""
Добавляет Match.tour — номер тура.

Зафиксировано 2026-08-21, пользователь: "непонятно сейчас из-за переносов
какой тур". KFF отдаёт номер тура прямо в /games/{id} (поле "tour", видели
в сыром JSON матча Окжетпес-Кызылжар: "tour": 23), просто раньше никто его
не читал и не сохранял. В отличие от start_time (который у перенесённого
матча становится ненадёжным ориентиром), номер тура не меняется вместе с
датой — единственный устойчивый признак "какой это был/будет тур".

Только AddField — существующие матчи получат tour=None до следующего
полного ресинка сезона (import_match_core теперь пишет это поле для
каждого матча, включая уже завершённые — sync_kff_premier_league/
sync_full_season проходят по ВСЕМ id сезона, не только по активным).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0004_match_manual_override'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='tour',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name='Тур',
            ),
        ),
    ]
