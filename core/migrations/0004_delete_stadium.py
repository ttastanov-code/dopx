# core/migrations/0004_delete_stadium.py
# Удаляет модель Stadium целиком (2026-09-09, решение пользователя, см.
# matches/migrations/0011_remove_match_stadium.py для контекста). Зависит
# от matches.0011 — FK на Match должен быть снят ДО удаления модели, на
# которую он ссылался, иначе миграция сломает граф зависимостей.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_stadium_needs_review'),
        ('matches', '0011_remove_match_stadium'),
    ]

    operations = [
        migrations.DeleteModel(
            name='Stadium',
        ),
    ]
