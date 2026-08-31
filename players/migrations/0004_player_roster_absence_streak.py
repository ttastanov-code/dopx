# players/migrations/0004_player_roster_absence_streak.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0003_player_kff_website_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='roster_absence_streak',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Считает подряд идущие проверки состава на kffleague.kz, где игрока не нашли — при достижении порога is_active снимается автоматически.',
                verbose_name='Подряд отсутствовал в составе на сайте KFF',
            ),
        ),
    ]
