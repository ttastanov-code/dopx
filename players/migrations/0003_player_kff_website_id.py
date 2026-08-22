# players/migrations/0003_player_kff_website_id.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0002_normalize_position_casing'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='kff_website_id',
            field=models.CharField(
                blank=True,
                help_text='Числовой id из URL kffleague.kz/ru/player/<id> — для скрапинга фото.',
                max_length=20,
                null=True,
                unique=True,
                verbose_name='ID игрока на сайте KFF',
            ),
        ),
    ]
