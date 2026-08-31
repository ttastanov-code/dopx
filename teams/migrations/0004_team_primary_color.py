# teams/migrations/0004_team_primary_color.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0003_team_kff_website_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='team',
            name='primary_color',
            field=models.CharField(
                blank=True,
                help_text='Например #1a2b3c — извлекается автоматически из логотипа, вручную менять не обязательно.',
                max_length=7,
                verbose_name='Фирменный цвет (HEX)',
            ),
        ),
    ]
