# players/migrations/0008_player_last_match_at.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('players', '0007_rename_player_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='last_match_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text='Start_time самой свежей фикстуры, из которой обновлялись team/number/position — используется как защита от отката этих полей при бэкафилле не по хронологии.',
                verbose_name='Дата последнего матча',
            ),
        ),
        migrations.AlterField(
            model_name='player',
            name='roster_absence_streak',
            field=models.PositiveIntegerField(
                default=0,
                help_text='Считает подряд идущие проверки состава на kffleague.kz, где игрока не нашли — при достижении порога is_active снимается автоматически. МЁРТВОЕ ПОЛЕ с 2026-09-09: parsers/kff удалён из проекта вместе со скрапером, который его инкрементировал (match_and_fetch_players_for_team/check_roster_departures) — ничего больше это поле не меняет. Оставлено как есть (не удалено), чтобы не терять историю на уже собранных данных.',
                verbose_name='Подряд отсутствовал в составе на сайте KFF',
            ),
        ),
    ]
