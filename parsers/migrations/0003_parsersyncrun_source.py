# Generated manually (см. комментарий в 0001_initial.py — makemigrations
# недоступен в этой песочнице без подключённой БД).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('parsers', '0002_parserdiscrepancy'),
    ]

    operations = [
        migrations.AddField(
            model_name='parsersyncrun',
            name='source',
            field=models.CharField(
                choices=[('kff', 'KFF'), ('sportmonks', 'Sportmonks')],
                db_index=True, default='kff', max_length=20, verbose_name='Источник',
            ),
        ),
    ]
