from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('lineups', '0004_alter_matchlineupplayer_field_position'),
    ]

    operations = [
        migrations.AlterField(
            model_name='matchlineupplayer',
            name='field_position',
            field=models.CharField(blank=True, help_text='Сторона на поле из ответа источника: C/L/R/LC/RC (не путать с амплуа). Нужна для слотов сборных.', max_length=5, verbose_name='Сторона на поле'),
        ),
    ]
