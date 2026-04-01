# users/migrations/0002_add_notification_settings.py
from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('users', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='notification_settings',
            field=models.JSONField(
                blank=True,
                default=dict,
                verbose_name='Настройки уведомлений'
            ),
        ),
    ]