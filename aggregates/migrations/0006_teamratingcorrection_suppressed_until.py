# aggregates/migrations/0006_teamratingcorrection_suppressed_until.py
"""
2026-08-28: mark_dismissed (users/admin.py::SuspiciousActivityFlagAdmin) обнулял
TeamRatingCorrection.correction, когда модератор отклонял флаг stats_divergence
как объяснимый, но не оставлял никакого "cooldown" — следующий суточный прогон
detect_rating_stats_divergence_task (aggregates/tasks.py) заново находил тот же
паттерн и заново перезаписывал correction, тихо отменяя решение модератора.
Добавляет suppressed_until — пока оно в будущем, _check_team_stats_divergence
пропускает команду, не перезаписывая её TeamRatingCorrection. См. докстринг
поля в aggregates/models.py.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('aggregates', '0005_teamratingcorrection'),
    ]

    operations = [
        migrations.AddField(
            model_name='teamratingcorrection',
            name='suppressed_until',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    'БАГ, КОТОРЫЙ ТУТ БЫЛ: mark_dismissed в users/admin.py обнулял correction, '
                    'но не оставлял никакого cooldown — следующий суточный прогон '
                    'detect_rating_stats_divergence_task (aggregates/tasks.py) заново находил тот же '
                    'паттерн и заново перезаписывал correction, тихо отменяя решение модератора. '
                    'Пока это поле в будущем, _check_team_stats_divergence пропускает команду, не трогая поправку.'
                ),
                verbose_name='Подавлено до',
            ),
        ),
    ]
