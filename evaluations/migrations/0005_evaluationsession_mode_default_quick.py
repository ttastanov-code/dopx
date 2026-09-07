# Generated manually (см. комментарий в 0004_evaluationsession_mode.py —
# makemigrations недоступен в этой песочнице без подключённой БД).
#
# Меняем только default нового поля 'mode' с 'full' на 'quick'
# (docs/adr/0031-quick-mode-primary-flow.md). Уже существующие строки
# не затрагиваются — Django AlterField с новым default не переписывает
# значения существующих записей, только default для будущих INSERT без
# явного mode.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0004_evaluationsession_mode'),
    ]

    operations = [
        migrations.AlterField(
            model_name='evaluationsession',
            name='mode',
            field=models.CharField(
                choices=[('full', 'Подробно'), ('quick', 'Быстро')],
                default='quick', max_length=10, verbose_name='Режим оценки',
            ),
        ),
    ]
