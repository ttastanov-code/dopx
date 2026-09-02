# Ручная миграция (см. коммент в users/migrations/0007_alter_userbadge_badge_type.py
# и 0011/0017 — Django в песочнице разработки недоступен, makemigrations
# нельзя прогнать локально). Добавляет 11 новых кодов достижений в choices
# `UserBadge.badge_type` (users/badges.py, продуктовый запрос "достижения
# по оценкам и прогнозам + супер-ультра уровень", 2026-09-01) — 6 обычных
# + 5 legendary. Список кодов — приложением НЕ проверяется на уровне БД
# (CharField.choices — валидация на уровне формы/clean, не CHECK-констрейнт),
# миграция нужна только чтобы состояние моделей совпадало с реальным кодом
# (иначе `manage.py makemigrations --check --dry-run` в CI найдёт
# расхождение — см. .github/workflows/deploy.yml).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0018_fix_last_evaluation_season_id_type'),
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
                    ('coach_expert', 'Тренерский эксперт'),
                    ('both_sides', 'Обе стороны'),
                    ('full_season', 'Полный сезон'),
                    ('stable_hand', 'Стабильная рука'),
                    ('derby_prophet', 'Дерби-пророк'),
                    ('against_the_tide', 'Против течения'),
                    ('perfect_tour', 'Идеальный тур'),
                    ('streak_250', 'Живая легенда трибун'),
                    ('prediction_streak_200', 'Абсолютный оракул'),
                    ('season_completionist', 'Стоглазый'),
                    ('max_trust', 'Максимальное доверие'),
                ],
                max_length=50,
                verbose_name='Тип достижения',
            ),
        ),
    ]
