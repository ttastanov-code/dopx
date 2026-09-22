# players/migrations/0010_player_name_source.py
# Ручная миграция (Django недоступен в песочнице для makemigrations, тот
# же паттерн, что и в dashboard/migrations/000X — см. коммент там).
# См. core/models.py::NAME_SOURCE_CHOICES — откуда взялось текущее ФИО,
# нужно, чтобы находить "угадано нашей транслитерацией" записи для
# проверки через Gemini (parsers/management/commands/verify_names_with_ai.py),
# 2026-09-22, прямая просьба пользователя после жалобы "Сергий Малий"
# вместо "Сергий Малый".
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0009_remove_player_players_pla_team_id_1a80c1_idx_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='name_source',
            field=models.CharField(blank=True, choices=[('clean_source', 'Кириллица от источника (Sportmonks)'), ('latin_foreign', 'Иностранное имя, оставлено латиницей'), ('guessed_transliteration', 'Угадано нашей транслитерацией'), ('ai_verified', 'Проверено ИИ, подтверждено staff'), ('manual', 'Правлено вручную')], db_index=True, max_length=30, verbose_name='Источник ФИО'),
        ),
    ]
