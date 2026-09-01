# matches/migrations/0009_match_was_rescheduled.py
"""
Добавляет Match.was_rescheduled — липкий флаг "дата матча существенно
отличается от дат остальных матчей его тура" (см. докстринг поля в
matches/models.py). НЕ путать со status='postponed': тот снимается
автосинком, как только KFF подтверждает окончательную дату (иначе
is_prediction_open() никогда не откроет приём прогнозов на такой матч).
Этот флаг — наоборот, никогда не снимается автоматически, это постоянная
историческая пометка поверх обычного статуса.

Миграция написана вручную (manage.py makemigrations недоступен в песочнице
разработки этого проекта, см. docs/BACKLOG.md) — простой AddField с
default=False, backfill не нужен (все существующие строки корректно
получают False).
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0008_alter_match_league_season_protect'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='was_rescheduled',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Дата матча существенно отличается от дат остальных матчей его тура — '
                    'признак того, что игру перенесли. Ставится автоматически и не '
                    'снимается синком; не влияет на текущий статус/доступность прогнозов.'
                ),
                verbose_name='Перенесён относительно своего тура',
            ),
        ),
    ]
