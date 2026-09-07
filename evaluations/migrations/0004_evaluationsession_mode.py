# Generated manually (см. комментарий в parsers/migrations/0001_initial.py —
# makemigrations недоступен в этой песочнице без подключённой БД).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('evaluations', '0003_evaluationsession_ip_address'),
    ]

    operations = [
        migrations.AddField(
            model_name='evaluationsession',
            name='mode',
            field=models.CharField(
                choices=[('full', 'Подробно'), ('quick', 'Быстро')],
                default='full', max_length=10, verbose_name='Режим оценки',
            ),
        ),
    ]
