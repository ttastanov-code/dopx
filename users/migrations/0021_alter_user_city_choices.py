# users/migrations/0021_alter_user_city_choices.py
# Ручная миграция (Django недоступен в песочнице для makemigrations, см.
# докстринги 0009/0011/... в dashboard/migrations — тот же приём).
# Добавляет choices на User.city (users/kz_cities.py::KZ_CITY_CHOICES) —
# 2026-09-23, продуктовый запрос "доработать город чтобы были реальные
# города всех пользователей". Choices — чисто Python-уровня валидация
# (та же оговорка, что и во всех предыдущих choices-миграциях этого
# проекта — "не enforced на уровне БД"), СУЩЕСТВУЮЩИЕ произвольные строки
# в уже заполненном поле НЕ ломаются и не удаляются этой миграцией.
from django.db import migrations, models

from users.kz_cities import KZ_CITY_CHOICES


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0020_suspiciousactivityflag_player_stats_divergence'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='city',
            field=models.CharField(blank=True, choices=KZ_CITY_CHOICES, max_length=120, verbose_name='Город'),
        ),
    ]
