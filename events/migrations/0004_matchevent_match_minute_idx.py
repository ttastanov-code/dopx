from django.db import migrations, models


class Migration(migrations.Migration):
    """
    Индекс (match, minute) на MatchEvent — отсутствовал, хотя это
    основной путь фильтрации/сортировки на странице матча (events/views.py::
    pulse_partial) и в апсерте парсера (parsers/kff/importers.py::
    import_events_and_minutes). См. docstring MatchEvent.Meta.indexes.
    """

    dependencies = [
        ('events', '0003_eventreaction'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='matchevent',
            index=models.Index(fields=['match', 'minute'], name='match_event_match_minute_idx'),
        ),
    ]
