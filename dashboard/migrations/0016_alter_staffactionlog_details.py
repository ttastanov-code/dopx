# dashboard/migrations/0016_alter_staffactionlog_details.py
# Ручная миграция (Django-окружение недоступно в этой сессии для
# makemigrations — та же ситуация, что и в 0009/0011/0012/0013/0015).
# Добавляет encoder=DjangoJSONEncoder на StaffActionLog.details, см.
# dashboard/models.py — фикс "Object of type datetime is not JSON
# serializable" при log_staff_action(details=...) с datetime внутри.
# Чисто Python-уровневое изменение (как сериализуется значение перед
# отправкой в БД), схему таблицы/колонку JSONField не трогает — никакого
# SQL эта миграция не выполняет.
from django.core.serializers.json import DjangoJSONEncoder
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dashboard', '0015_alter_staffactionlog_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='staffactionlog',
            name='details',
            field=models.JSONField(blank=True, default=dict, encoder=DjangoJSONEncoder, verbose_name='Детали'),
        ),
    ]
