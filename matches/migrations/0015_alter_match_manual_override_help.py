from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0014_matchreaction'),
    ]

    operations = [
        migrations.AlterField(
            model_name='match',
            name='manual_override',
            field=models.BooleanField(default=False, help_text='Включите, если правили статус/дату вручную. Пока включено, автосинк не трогает статус и дату матча.', verbose_name='Статус вручную (не трогать автосинком)'),
        ),
    ]
