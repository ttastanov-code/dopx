# Ручная миграция (см. коммент в users/migrations/0007_alter_userbadge_badge_type.py
# и 0011 — Django в песочнице разработки недоступен, makemigrations нельзя
# прогнать локально). Отражает переименование двух бейджей серии оценок
# из users/badges.py в рамках пересчёта серии по турам чемпионата вместо
# календарных дней (2026-08-31, см. докстринг User.evaluation_streak):
# streak_7 "Неделя подряд" → "Верный трибунам", streak_30 "Месяц подряд" →
# "Сезонный болельщик". Список кодов и порядок — без изменений, это чисто
# смена отображаемых label'ов в choices.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0016_streak_by_tour_and_correctness'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userbadge',
            name='badge_type',
            field=models.CharField(
                choices=[
                    ('first_evaluation', 'Первая оценка'),
                    ('active_fan_10', 'Активный фанат'),
                    ('active_fan_50', 'Хардкор фанат'),
                    ('active_fan_150', 'Легенда трибун'),
                    ('streak_7', 'Верный трибунам'),
                    ('streak_30', 'Сезонный болельщик'),
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
