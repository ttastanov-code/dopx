# teams/migrations/0008_alter_team_logo_alter_team_logo_url.py
# Косметика (тот же паттерн, что dashboard/migrations/0002-0007 — ручная
# миграция, Django недоступен в песочнице для makemigrations): только
# verbose_name/help_text у Team.logo и Team.logo_url, на схему БД и данные
# не влияет. Сделано 2026-09-09 по вопросу пользователя про автообновление
# гербов — см. комментарии в teams/models.py::Team.logo_display и
# parsers/sportmonks/importers.py::get_or_create_team для сути изменения
# (logo теперь staff-override с приоритетом, logo_url свободно
# перезаписывается синком Sportmonks).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0007_team_sportmonks_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='team',
            name='logo',
            field=models.ImageField(
                blank=True, null=True, upload_to='teams/',
                verbose_name='Логотип (ручная загрузка)',
                help_text=(
                    'Если загружен — всегда показывается вместо герба из '
                    'Sportmonks и НИКОГДА не перезаписывается синком. '
                    'Используйте, если герб источника устарел/неверен.'
                ),
            ),
        ),
        migrations.AlterField(
            model_name='team',
            name='logo_url',
            field=models.URLField(
                blank=True, null=True,
                verbose_name='URL логотипа (Sportmonks)',
                help_text=(
                    'Заполняется и обновляется автоматически при каждом '
                    'синке Sportmonks. Не редактируйте вручную — правки '
                    'затрутся при следующем синке; для ручного герба '
                    'используйте поле выше.'
                ),
            ),
        ),
    ]
