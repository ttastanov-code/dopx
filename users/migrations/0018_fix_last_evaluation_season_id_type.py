# users/migrations/0018_fix_last_evaluation_season_id_type.py
# Фикс бага из 0016_streak_by_tour_and_correctness: last_evaluation_season_id
# был объявлен как IntegerField в неверном предположении, что Match.season_id
# — числовой id. Season (seasons/models.py) — потомок core.models.BaseModel,
# первичный ключ там UUIDField, не автоинкремент. Любая попытка записать
# сюда реальный match.season_id падала в психопг с DataError "integer out of
# range" (UUID как 128-битное число не лезет в 32-битный integer) — см.
# users/models.py::User.update_evaluation_stats, вызывается из
# evaluations/views.py::EvaluateMatchFinalView на последнем шаге вайзарда.
#
# Столбец безопасно перевести напрямую: раз запись реального значения
# всегда падала внутри transaction.atomic() (см. форма_valid во
# EvaluateMatchFinalView), ни у одного пользователя там не могло осесть
# ничего, кроме NULL — переключение типа не теряет данных.
#
# ИСПРАВЛЕНО: первая версия миграции использовала AlterField напрямую —
# Postgres генерирует `ALTER COLUMN ... TYPE uuid USING col::uuid`, а
# implicit-каста `integer -> uuid` в Postgres просто НЕТ (даже для столбца
# из одних NULL — ошибка ловится на этапе разбора выражения `::uuid`,
# раньше, чем Postgres успевает заметить, что реальных значений там нет).
# RemoveField+AddField — DROP COLUMN и следом ADD COLUMN, без каста вообще
# (то же самое `python manage.py migrate` уже один раз откатил как единую
# транзакцию — упавшая AlterField не оставила частичных изменений).
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0017_alter_userbadge_badge_type'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='user',
            name='last_evaluation_season_id',
        ),
        migrations.AddField(
            model_name='user',
            name='last_evaluation_season_id',
            field=models.UUIDField(blank=True, null=True, verbose_name='Сезон последней оценки'),
        ),
    ]
