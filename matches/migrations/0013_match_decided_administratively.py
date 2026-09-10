# matches/migrations/0013_match_decided_administratively.py
# Новое поле Match.decided_administratively (2026-09-10, расследование
# алерта sync_monitoring "12 матчей без составов за 24ч") — см. её
# докстринг в matches/models.py и правку в parsers/tasks.py::
# check_sync_errors_and_alert.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0012_matchteamstatistics_dangerous_attacks'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='decided_administratively',
            field=models.BooleanField(
                default=False,
                help_text='Матч завершён административным решением (неявка, техническое поражение, прерван и засчитан) — у источника данных никогда не будет состава и событий для такого матча, это не ошибка синка.',
                verbose_name='Решён технически (неявка/тех. поражение)',
            ),
        ),
    ]
