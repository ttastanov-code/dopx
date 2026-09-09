# matches/migrations/0011_remove_match_stadium.py
# Убирает Match.stadium (2026-09-09, решение пользователя) — venue-данные
# Sportmonks для КПЛ принципиально ненадёжны (клубы играют "домашние"
# матчи на разных стадионах в разных городах в течение одного сезона).
# Должна выполниться ПЕРЕД core.0004_delete_stadium (сначала снимаем FK,
# потом удаляем саму модель Stadium) — см. зависимость ниже.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0010_match_sportmonks_id_referee_crew'),
        ('core', '0003_stadium_needs_review'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='match',
            name='stadium',
        ),
    ]
