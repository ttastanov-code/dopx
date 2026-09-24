from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0008_aggregate_rating_correction_applied'),
    ]

    operations = [
        migrations.AlterField(
            model_name='teamratingcorrection',
            name='suppressed_until',
            field=models.DateTimeField(blank=True, help_text='Пока дата в будущем, детектор расхождения не трогает поправку команды (флаг отклонён модератором).', null=True, verbose_name='Подавлено до'),
        ),
        migrations.AlterField(
            model_name='playerratingcorrection',
            name='suppressed_until',
            field=models.DateTimeField(blank=True, help_text='Пока дата в будущем, детектор расхождения не трогает поправку игрока (флаг отклонён модератором).', null=True, verbose_name='Подавлено до'),
        ),
    ]
