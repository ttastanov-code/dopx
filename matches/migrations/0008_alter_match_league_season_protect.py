# matches/migrations/0008_alter_match_league_season_protect.py
"""
Меняет on_delete League/Season на Match: CASCADE -> PROTECT.

БАГ, КОТОРЫЙ ТУТ БЫЛ: League/Season — справочные сущности, а на них стоял
on_delete=CASCADE. Удаление ОДНОЙ League/Season сносило ВСЕ матчи этой
лиги/сезона, а вместе с ними каскадно — события, составы, статистику,
оценки и всё остальное, что висит на Match. PROTECT не даёт удалить
League/Season, пока на них ссылается хотя бы один Match — единственно
разумное поведение для справочника: удалять сезон/лигу с матчами внутри
не должно быть возможно "по ошибке одним кликом", такие вещи (если вообще
нужны) — отдельное осознанное действие с явной чисткой матчей сначала.

Миграция написана вручную (см. docs/BACKLOG.md про manage.py makemigrations
недоступен в песочнице разработки) по образцу matches/migrations/0005_match_tour.py
и 0001_initial.py — только AlterField, схема БД (колонки/constraint'ы) не
меняется, on_delete — поведение уровня Django ORM, а не БД (нет DB-level FK
ON DELETE в этом проекте, см. 0001_initial.py).
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0007_rename_matches_mat_team_id_5f2e91_idx_matches_mat_team_id_bcf70f_idx'),
    ]

    operations = [
        migrations.AlterField(
            model_name='match',
            name='league',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to='leagues.league',
                verbose_name='Лига',
            ),
        ),
        migrations.AlterField(
            model_name='match',
            name='season',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                to='seasons.season',
                verbose_name='Сезон',
            ),
        ),
    ]
