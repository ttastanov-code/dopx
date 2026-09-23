# core/migrations/0005_platformsetting.py
# Ручная миграция (см. докстринг core/models.py::PlatformSetting) — новая
# модель для рантайм-конфига платформы, редактируемого в staff-дашборде
# без деплоя (dashboard/views.py::platform_settings).
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('core', '0004_delete_stadium'),
    ]

    operations = [
        migrations.CreateModel(
            name='PlatformSetting',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('key', models.CharField(max_length=100, unique=True, verbose_name='Ключ')),
                ('value', models.TextField(blank=True, verbose_name='Значение')),
                ('value_type', models.CharField(choices=[('string', 'Текст'), ('int', 'Целое число'), ('float', 'Дробное число'), ('bool', 'Да/Нет')], default='string', max_length=10, verbose_name='Тип')),
                ('description', models.TextField(blank=True, help_text='Что это значение делает и на что влияет — показывается прямо в форме редактирования.', verbose_name='Описание')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='Изменено')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Кем изменено')),
            ],
            options={
                'verbose_name': 'Настройка платформы',
                'verbose_name_plural': 'Настройки платформы',
                'ordering': ['key'],
            },
        ),
    ]
