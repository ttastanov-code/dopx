# Ручная миграция (в песочнице разработки нет доступа к manage.py
# makemigrations — см. коммент в users/migrations/0007_alter_userbadge_badge_type.py
# и аналогичные миграции по всему проекту).
#
# Продуктовая правка 2026-08-31 (жалоба пользователя: "серия оценок это
# тупо, потому что матчи только 2 дня в неделю и серия не сдвинется ни у
# кого" + "серию прогнозов надо беспрерывную правильную показывать"):
#
#   evaluation_streak — раньше считался по КАЛЕНДАРНЫМ ДНЯМ подряд
#   (last_evaluation_date), теперь по ТУРАМ подряд в одном сезоне
#   (last_evaluation_season_id + last_evaluation_tour). См. докстринг
#   User.evaluation_streak/update_evaluation_stats в users/models.py.
#
#   prediction_streak — раньше считался по КАЛЕНДАРНЫМ ДНЯМ ставки прогноза
#   (last_prediction_date), теперь по ПОДРЯД УГАДАННЫМ исходам (растёт на
#   +1 за верный прогноз, сбрасывается в 0 при первом неверном). Дата
#   последней ставки для этой логики больше не нужна — last_prediction_date
#   удаляется целиком. См. докстринг User.prediction_streak/
#   update_prediction_stats в users/models.py.
#
# last_evaluation_date/last_prediction_date удаляются (не переиспользуются
# больше нигде в проекте — см. грep перед миграцией), а не остаются мёртвым
# столбцом: обе колонки хранили дату по старой, уже неверной семантике,
# оставлять их значило бы держать в БД данные, которые ничего не значат.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0015_suspiciousactivityflag_stats_divergence_source'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='user',
            name='last_evaluation_date',
        ),
        migrations.RemoveField(
            model_name='user',
            name='last_prediction_date',
        ),
        migrations.AddField(
            model_name='user',
            name='last_evaluation_season_id',
            field=models.IntegerField(blank=True, null=True, verbose_name='Сезон последней оценки'),
        ),
        migrations.AddField(
            model_name='user',
            name='last_evaluation_tour',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='Тур последней оценки'),
        ),
        migrations.AlterField(
            model_name='user',
            name='prediction_streak',
            field=models.IntegerField(default=0, verbose_name='Серия угаданных прогнозов'),
        ),
    ]
