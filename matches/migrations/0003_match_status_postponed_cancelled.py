# matches/migrations/0003_match_status_postponed_cancelled.py
"""
Добавляет "postponed"/"cancelled" в STATUS_CHOICES матча. Только метаданные
choices на CharField — на самих данных в БД ничего не меняет (при желании
безопасно накатывать/откатывать).

Раньше STATUS_MAP (parsers/kff/importers.py) схлопывал оба этих статуса
KFF в "scheduled"/"finished" соответственно, потому что таких значений
вообще не было в choices — сайт не мог показать пользователю "матч
перенесён", а стартовая дата никогда не пересинхронизировалась
(update_match_statuses её не трогал). См. docs/BACKLOG.md, разбор от
2026-08-18.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0002_match_matches_mat_status_2e1034_idx'),
    ]

    operations = [
        migrations.AlterField(
            model_name='match',
            name='status',
            field=models.CharField(
                choices=[
                    ('scheduled', 'Запланирован'),
                    ('live', 'Идёт'),
                    ('finished', 'Завершён'),
                    ('postponed', 'Перенесён'),
                    ('cancelled', 'Отменён'),
                ],
                default='scheduled',
                max_length=20,
                verbose_name='Статус',
            ),
        ),
    ]
