# players/migrations/0006_rename_playersidelined_index.py
"""
ИСПРАВЛЕНИЕ БАГА, НАЙДЕННОГО ПОЛЬЗОВАТЕЛЕМ СРАЗУ ПОСЛЕ 0005 (2026-09-08):
`manage.py migrate` применил 0005 без ошибок, но `manage.py migrate` в
ЭТОМ ЖЕ прогоне вывел "Your models in app(s): 'players' have changes that
are not yet reflected in a migration" — модель PlayerSidelined в
players/models.py объявляла индекс `models.Index(fields=['player',
'end_date'])` БЕЗ явного имени, а миграция 0005 вручную угадала имя,
которое Django присвоил бы автогенерацией
(`players_pla_player__f7d1d1_idx`) — угаданный хеш разошёлся с тем, что
реально считает Django, поэтому makemigrations продолжал видеть
"неприменённое изменение" на пустом месте.

Исправление по образцу events.models.MatchEvent/EventReaction (там индексы
с самого начала именуются явно, специально по той же причине — миграции в
проекте пишутся вручную без доступа к БД для makemigrations, см.
docs/BACKLOG.md): модель обновлена — индекс теперь называется явно
`player_sidelined_end_date_idx`. RenameIndex с `old_fields` — штатный
Django-способ переименовать индекс, который был создан БЕЗ явного имени
(Django сам находит его в БД по составу полей, не по угаданному имени) —
безопаснее, чем RemoveIndex+AddIndex (не пересоздаёт индекс с нуля).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0005_player_sportmonks_id_playersidelined'),
    ]

    operations = [
        migrations.RenameIndex(
            model_name='playersidelined',
            new_name='player_sidelined_end_date_idx',
            old_fields=('player', 'end_date'),
        ),
    ]
