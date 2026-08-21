# matches/migrations/0004_match_manual_override.py
"""
Добавляет Match.manual_override — флаг "не трогать этот матч автосинком".

Зафиксировано 2026-08-21: KFF публикует "перенесён на неопределённый срок"
текстовым баннером на странице матча ЗАДОЛГО до того, как структурные поля
status/date в их API реально меняются (проверено пользователем вручную на
kff.kz — дата и статус конкретного матча остаются старыми, баннер живёт
отдельно). Раньше ручная правка статуса в админке была бы молча откачена
следующим циклом update_match_statuses — see parsers/tasks.py, STATUS_MAP.get
всегда переписывал бы status обратно в "scheduled", потому что api_status
у KFF ещё не сменился. См. docs/BACKLOG.md.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0003_match_status_postponed_cancelled'),
    ]

    operations = [
        migrations.AddField(
            model_name='match',
            name='manual_override',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'Включите, если поправили статус/дату матча вручную раньше, чем '
                    'KFF обновил официальные данные (например, KFF уже показывает баннер '
                    '"перенесён", но дата в их API ещё старая). Пока включено, '
                    'автоматическая синхронизация не трогает статус и дату этого матча.'
                ),
                verbose_name='Статус вручную (не трогать автосинком)',
            ),
        ),
    ]
