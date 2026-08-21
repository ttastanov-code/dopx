# Ручная миграция (в песочнице разработки нет доступа к manage.py
# makemigrations — см. коммент в users/migrations/0007_alter_userbadge_badge_type.py
# и аналогичные миграции по всему проекту). Retention loop "Серии",
# 2026-08-21: prediction_streak/last_prediction_date на User (прямой аналог
# evaluation_streak/last_evaluation_date) + расширение choices badge_type
# четырьмя новыми кодами из users/badges.py (first_prediction,
# prediction_streak_7/30/100).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_pushsubscription'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='prediction_streak',
            field=models.IntegerField(default=0, verbose_name='Серия прогнозов'),
        ),
        migrations.AddField(
            model_name='user',
            name='last_prediction_date',
            field=models.DateField(blank=True, null=True, verbose_name='Последний прогноз'),
        ),
        migrations.AlterField(
            model_name='userbadge',
            name='badge_type',
            field=models.CharField(
                choices=[
                    ('first_evaluation', 'Первая оценка'),
                    ('active_fan_10', 'Активный фанат'),
                    ('active_fan_50', 'Хардкор фанат'),
                    ('active_fan_150', 'Легенда трибун'),
                    ('streak_7', 'Неделя подряд'),
                    ('streak_30', 'Месяц подряд'),
                    ('streak_100', 'Железная дисциплина'),
                    ('accurate_analyst', 'Точный аналитик'),
                    ('foresight', 'Провидец'),
                    ('bias_free', 'Без предвзятости'),
                    ('early_bird', 'Ранняя пташка'),
                    ('judge_of_judges', 'Судья судей'),
                    ('polyglot', 'Полиглот лиги'),
                    ('first_prediction', 'Первый прогноз'),
                    ('prediction_streak_7', 'Аналитик недели'),
                    ('prediction_streak_30', 'Штатный прогнозист'),
                    ('prediction_streak_100', 'Оракул трибун'),
                    ('derby_hunter', 'Дерби-эксперт'),
                    ('monthly_champion', 'Чемпион месяца'),
                    ('founder', 'Первопроходец'),
                ],
                max_length=50,
                verbose_name='Тип достижения',
            ),
        ),
    ]
