from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0011_potentialduplicateplayer'),
    ]

    operations = [
        migrations.AlterField(
            model_name='player',
            name='roster_absence_streak',
            field=models.PositiveIntegerField(default=0, help_text='Устаревшее поле старого скрапера KFF, больше не обновляется.', verbose_name='Подряд отсутствовал в составе на сайте KFF'),
        ),
    ]
