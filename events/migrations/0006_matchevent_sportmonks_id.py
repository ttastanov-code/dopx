# events/migrations/0006_matchevent_sportmonks_id.py
# Ручная миграция (см. коммент в 0005_alter_matchevent_event_type.py —
# Django недоступен в песочнице разработки для makemigrations).
#
# Добавляет MatchEvent.sportmonks_id + индекс, и СРАЗУ ЖЕ backfill'ит его
# для уже существующих строк из extra_data['id'] (это поле всегда
# сохранялось — см. import_events: `extra_data=evt`, просто раньше никогда
# не читалось обратно). Backfill обязателен, не косметика: parsers/
# sportmonks/importers.py::import_events теперь сопоставляет повторный
# импорт СНАЧАЛА по sportmonks_id — без бэкафилла все уже отслеживаемые
# live-матчи при следующем же цикле опроса создали бы дубли каждого своего
# события (совпадение по id не нашлось бы, а поле было бы NULL). См.
# докстринг MatchEvent.sportmonks_id и import_events для полного разбора
# бага (VAR меняет жёлтую на красную тому же событию — старая запись
# зависала в ленте навсегда).
from django.db import migrations, models


def backfill_sportmonks_id(apps, schema_editor):
    MatchEvent = apps.get_model('events', 'MatchEvent')
    db_alias = schema_editor.connection.alias
    queryset = MatchEvent.objects.using(db_alias).exclude(extra_data={}).only('id', 'extra_data')
    to_update = []
    for event in queryset.iterator():
        raw_id = (event.extra_data or {}).get('id')
        if raw_id is not None:
            event.sportmonks_id = str(raw_id)
            to_update.append(event)
            if len(to_update) >= 500:
                MatchEvent.objects.using(db_alias).bulk_update(to_update, ['sportmonks_id'])
                to_update = []
    if to_update:
        MatchEvent.objects.using(db_alias).bulk_update(to_update, ['sportmonks_id'])


def noop_reverse(apps, schema_editor):
    # Откат AddField сам обнулит поле — отдельная очистка не нужна.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0005_alter_matchevent_event_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='matchevent',
            name='sportmonks_id',
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
        migrations.AddIndex(
            model_name='matchevent',
            index=models.Index(fields=['match', 'sportmonks_id'], name='match_event_sportmonks_id_idx'),
        ),
        migrations.RunPython(backfill_sportmonks_id, noop_reverse),
    ]
