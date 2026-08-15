# Generated manually — см. комментарий в
# analytics/migrations/0001_initial.py про отсутствие сетевого доступа к
# PyPI в песочнице разработки. Одно поле, тривиально проверить глазами.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0007_alter_userbadge_badge_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='is_profile_public',
            field=models.BooleanField(
                default=True,
                help_text='Если выключено — /u/<username>/ отдаёт 404 для всех, кроме вас самих',
                verbose_name='Публичный профиль',
            ),
        ),
    ]
