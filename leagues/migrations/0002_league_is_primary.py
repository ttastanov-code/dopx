# leagues/migrations/0002_league_is_primary.py
"""
Добавляет League.is_primary + помечает главной лигой ту, что фактически
уже единственная активна на сайте — иначе core/views.py::standings_preview
после следующего деплоя внезапно перестал бы находить турнирную таблицу
(is_primary=False у всех). См. docs/BACKLOG.md, находка 1.
"""
from django.db import migrations, models


def mark_existing_league_as_primary(apps, schema_editor):
    League = apps.get_model('leagues', 'League')
    # Приоритет: лига с активным сезоном (если есть) — она и есть та, что
    # реально показывается на главной сегодня. Если активного сезона нет
    # ни у кого (свежая БД) — берём первую лигу как разумный дефолт,
    # staff всегда может переключить в админке.
    Season = apps.get_model('seasons', 'Season')
    active_season = Season.objects.filter(is_active=True).order_by('-year').first()
    if active_season:
        league = active_season.league
    else:
        league = League.objects.order_by('name').first()

    if league:
        league.is_primary = True
        league.save(update_fields=['is_primary'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('leagues', '0001_initial'),
        ('seasons', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='league',
            name='is_primary',
            field=models.BooleanField(
                default=False,
                help_text='Турнирная таблица какой лиги показывается на главной странице. Должна быть ровно одна.',
                verbose_name='Главная лига сайта',
            ),
        ),
        migrations.RunPython(mark_existing_league_as_primary, noop_reverse),
    ]
