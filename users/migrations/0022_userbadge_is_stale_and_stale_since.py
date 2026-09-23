# users/migrations/0022_userbadge_is_stale_and_stale_since.py
# Ручная миграция (Django недоступен в песочнице для makemigrations, см.
# докстринги 0009/0011/.../0021 — тот же приём).
# 2026-09-23, фикс аудита: добавляет UserBadge.is_stale/stale_since —
# механизм пометки "утратил актуальность" для 5 статусных бейджей
# (foresight/max_trust/stable_hand/accurate_analyst/bias_free), см.
# докстринг поля is_stale в users/models.py::UserBadge и
# users/services.py::STATUS_BADGE_TYPES / revalidate_status_badges.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0021_alter_user_city_choices'),
    ]

    operations = [
        migrations.AddField(
            model_name='userbadge',
            name='is_stale',
            field=models.BooleanField(
                default=False,
                help_text='Только для статусных достижений — показатель упал ниже порога после получения бейджа.',
                verbose_name='Утратил актуальность',
            ),
        ),
        migrations.AddField(
            model_name='userbadge',
            name='stale_since',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Утратил актуальность с'),
        ),
    ]
