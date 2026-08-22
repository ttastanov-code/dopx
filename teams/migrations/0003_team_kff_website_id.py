# teams/migrations/0003_team_kff_website_id.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0002_team_rivals'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='kff_website_id',
            field=models.CharField(
                blank=True,
                help_text='Числовой id из URL kffleague.kz/ru/team/<id> — для скрапинга фото игроков.',
                max_length=20,
                null=True,
                unique=True,
                verbose_name='ID команды на сайте KFF',
            ),
        ),
    ]
