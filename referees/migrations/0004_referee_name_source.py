# referees/migrations/0004_referee_name_source.py
# См. players/migrations/0010_player_name_source.py — тот же коммент,
# то же новое поле, для судей.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('referees', '0003_referee_sportmonks_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='referee',
            name='name_source',
            field=models.CharField(blank=True, choices=[('clean_source', 'Кириллица от источника (Sportmonks)'), ('latin_foreign', 'Иностранное имя, оставлено латиницей'), ('guessed_transliteration', 'Угадано нашей транслитерацией'), ('ai_verified', 'Проверено ИИ, подтверждено staff'), ('manual', 'Правлено вручную')], db_index=True, max_length=30, verbose_name='Источник ФИО'),
        ),
    ]
