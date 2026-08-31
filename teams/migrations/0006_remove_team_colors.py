# teams/migrations/0006_remove_team_colors.py
"""
Откат фичи "автоцвет клубов" (миграции 0004/0005) — продуктовое решение
2026-08-31: градиент по цветам команд в hero-баннере выглядел неаккуратно,
решили вернуть окраску только по статусу матча (live/завершён/...) и
сделать премиальный дизайн через логотипы-тиснение вместо цвета. Поля
Team.primary_color/secondary_color удаляются целиком вместе со всей
инфраструктурой, которая их считала (teams/services.py, teams/tasks.py,
teams/signals.py, management-команда compute_team_colors — удалены).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('teams', '0005_team_secondary_color'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='team',
            name='primary_color',
        ),
        migrations.RemoveField(
            model_name='team',
            name='secondary_color',
        ),
    ]
