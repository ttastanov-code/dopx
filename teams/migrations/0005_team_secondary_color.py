# teams/migrations/0005_team_secondary_color.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0004_team_primary_color'),
    ]

    operations = [
        migrations.AlterField(
            model_name='team',
            name='primary_color',
            field=models.CharField(
                blank=True,
                help_text='Например #1a2b3c — извлекается автоматически из логотипа, вручную менять не обязательно.',
                max_length=7,
                verbose_name='Основной фирменный цвет (HEX)',
            ),
        ),
        migrations.AddField(
            model_name='team',
            name='secondary_color',
            field=models.CharField(
                blank=True,
                help_text='Второй цвет двухцветной эмблемы — тоже извлекается автоматически, может быть пустым для однотонных логотипов.',
                max_length=7,
                verbose_name='Второй фирменный цвет (HEX)',
            ),
        ),
    ]
