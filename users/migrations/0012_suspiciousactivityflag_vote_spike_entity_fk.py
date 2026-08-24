# users/migrations/0012_suspiciousactivityflag_vote_spike_entity_fk.py
"""
Anti-brigading, 2026-08-23: SuspiciousActivityFlag.user становится nullable
и добавляется generic FK (content_type/object_id) — новый источник
"vote_spike" (aggregates/tasks.py::detect_vote_velocity_anomalies_task)
сигнализирует про АНОМАЛИЮ У СУЩНОСТИ (игрок/команда/тренер получили
статистически выбивающийся всплеск экстремальных оценок), а не про
конкретного пользователя — см. докстринг модели в users/models.py.
"""
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0011_prediction_streak_and_badges'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.AlterField(
            model_name='suspiciousactivityflag',
            name='user',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='suspicious_activity_flags',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Пользователь',
            ),
        ),
        migrations.AlterField(
            model_name='suspiciousactivityflag',
            name='source',
            field=models.CharField(
                choices=[
                    ('fast_wizard', 'Слишком быстрое заполнение вайзарда оценки'),
                    ('ip_cluster', 'Кластер аккаунтов с одного IP'),
                    ('extreme_bias', 'Экстремальная историческая предвзятость'),
                    ('vote_spike', 'Аномальный всплеск голосования (возможный сговор)'),
                    ('manual', 'Отмечено вручную модератором'),
                ],
                max_length=30,
                verbose_name='Источник сигнала',
            ),
        ),
        migrations.AddField(
            model_name='suspiciousactivityflag',
            name='content_type',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to='contenttypes.contenttype',
                verbose_name='Тип сущности',
            ),
        ),
        migrations.AddField(
            model_name='suspiciousactivityflag',
            name='object_id',
            field=models.CharField(blank=True, max_length=64, null=True, verbose_name='ID сущности'),
        ),
        migrations.AddIndex(
            model_name='suspiciousactivityflag',
            index=models.Index(
                fields=['content_type', 'object_id', 'status'],
                name='users_suspi_content_9e1f3a_idx',
            ),
        ),
    ]
