# players/migrations/0007_rename_player_indexes.py
"""
ИСПРАВЛЕНИЕ БАГА, НАЙДЕННОГО ПОЛЬЗОВАТЕЛЕМ (2026-09-09): чистый прогон
`manage.py migrate` (все миграции уже применены, "No migrations to
apply") всё равно вывел "Your models in app(s): 'players' have changes
that are not yet reflected in a migration".

ТОЧНО ТА ЖЕ причина, что уже чинили для PlayerSidelined в
0006_rename_playersidelined_index.py — только на этот раз для ДВУХ
индексов модели Player из 0001_initial.py, которые тогда пропустили.
Player.Meta.indexes объявляла индексы БЕЗ явного имени:
    models.Index(fields=['team', 'is_active'])
    models.Index(fields=['last_name', 'first_name'])
0001_initial вручную угадал автосгенерированные Django-хеши
(players_pla_team_id_1a80c1_idx / players_pla_last_na_1786cf_idx) — угаданные
хеши разошлись с тем, что реально считает Django, поэтому makemigrations
продолжал видеть "неприменённое изменение" на пустом месте при каждом
прогоне, даже когда в БД физически всё уже применено.

Модель обновлена — оба индекса теперь называются явно
(player_team_active_idx / player_last_first_name_idx). RenameIndex с
old_fields — штатный Django-способ переименовать индекс, который был
создан БЕЗ явного имени: Django сам находит его в БД по составу полей,
а не по угаданному в 0001 имени — безопасно вне зависимости от того,
насколько неправильным был исходный угаданный хеш.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0006_rename_playersidelined_index'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='player',
            new_name='player_team_active_idx',
            old_fields=('team', 'is_active'),
        ),
        migrations.RenameIndex(
            model_name='player',
            new_name='player_last_first_name_idx',
            old_fields=('last_name', 'first_name'),
        ),
    ]
