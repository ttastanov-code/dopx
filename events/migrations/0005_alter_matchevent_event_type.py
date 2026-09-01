# events/migrations/0005_alter_matchevent_event_type.py
# Ручная миграция (Django недоступен в песочнице разработки — см. коммент
# в users/migrations/0007_alter_userbadge_badge_type.py и аналогичные
# миграции по всему проекту). Добавляет 'disallowed_goal' в choices
# MatchEvent.event_type — см. докстринг у EVENT_TYPES в events/models.py.
# На схему БД не влияет (choices не enforced на уровне колонки), только
# на state миграций/label в админке и шаблонах.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0004_matchevent_match_minute_idx'),
    ]

    operations = [
        migrations.AlterField(
            model_name='matchevent',
            name='event_type',
            field=models.CharField(
                choices=[
                    ('goal', 'Гол'),
                    ('yellow_card', 'Жёлтая карточка'),
                    ('red_card', 'Красная карточка'),
                    ('substitution', 'Замена'),
                    ('penalty', 'Пенальти'),
                    ('own_goal', 'Автогол'),
                    ('var_check', 'VAR проверка'),
                    ('disallowed_goal', 'Гол отменён'),
                ],
                max_length=20,
            ),
        ),
    ]
