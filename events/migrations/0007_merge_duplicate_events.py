# Склейка дублей событий с одним id поставщика (уникальность — в 0008: Postgres не меняет таблицу после удалений в той же транзакции).
from django.db import migrations
from django.db.models import Count


def merge_duplicates(apps, schema_editor):
    """Оставляем самую свежую версию события; реакции, голоса за перелом и опросы переносим на неё."""
    MatchEvent = apps.get_model("events", "MatchEvent")
    EventReaction = apps.get_model("events", "EventReaction")
    MatchEvaluation = apps.get_model("evaluations", "MatchEvaluation")
    DailyPoll = apps.get_model("engagement", "DailyPoll")

    dupes = (MatchEvent.objects.exclude(sportmonks_id__isnull=True).values("match_id", "sportmonks_id")
             .annotate(n=Count("id")).filter(n__gt=1))
    for row in dupes:
        events = list(MatchEvent.objects.filter(match_id=row["match_id"], sportmonks_id=row["sportmonks_id"])
                      .order_by("-updated_at", "-created_at"))
        keep, extra = events[0], events[1:]
        for old in extra:
            taken = set(EventReaction.objects.filter(match_event=keep).values_list("user_id", flat=True))
            EventReaction.objects.filter(match_event=old).exclude(user_id__in=taken).update(match_event=keep)
            MatchEvaluation.objects.filter(turning_point_event=old).update(turning_point_event=keep)
            DailyPoll.objects.filter(event=old).update(event=keep)
            old.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("events", "0006_matchevent_sportmonks_id"),
        ("evaluations", "0008_evaluationsession_pending_xp"),
        ("engagement", "0006_season_pass_abonement"),
    ]

    operations = [
        migrations.RunPython(merge_duplicates, migrations.RunPython.noop),
    ]
