from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='playermatchaggregate',
            name='own_fans_avg',
            field=models.FloatField(
                blank=True,
                help_text='avg(contribution) от зрителей, поддержавших команду игрока',
                null=True,
                verbose_name='Средняя оценка от фанатов игрока',
            ),
        ),
        migrations.AddField(
            model_name='playermatchaggregate',
            name='rival_fans_avg',
            field=models.FloatField(
                blank=True,
                help_text='avg(contribution) от зрителей, поддержавших команду-соперника',
                null=True,
                verbose_name='Средняя оценка от фанатов соперника',
            ),
        ),
        migrations.AddField(
            model_name='playermatchaggregate',
            name='neutral_avg',
            field=models.FloatField(
                blank=True,
                help_text='avg(contribution) от зрителей без выбранной стороны/контекста',
                null=True,
                verbose_name='Средняя оценка от нейтральных зрителей',
            ),
        ),
    ]
