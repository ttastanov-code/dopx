# parsers/migrations/0004_name_ai_verification.py
# Ручная миграция (Django недоступен в песочнице для makemigrations, тот же
# паттерн, что и в остальных ручных миграциях этой сессии). Новые модели
# NameVerificationSuggestion (очередь на проверку/подтверждение staff,
# generic FK на Player/Referee/Coach — тот же паттерн, что
# users.models.SuspiciousActivityFlag, см. users/migrations/0012_...) и
# ConfirmedNameCorrection (DB-версия parsers/sportmonks/name_translations.py::
# PLAYER_NAME_CORRECTIONS — поправки ИИ вступают в силу без деплоя).
# 2026-09-22, прямая просьба пользователя после жалобы "Сергий Малий"
# вместо "Сергий Малый".
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('contenttypes', '0002_remove_content_type_name'),
        ('parsers', '0003_parsersyncrun_source'),
    ]

    operations = [
        migrations.CreateModel(
            name='NameVerificationSuggestion',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('object_id', models.CharField(max_length=64, verbose_name='ID сущности')),
                ('entity_label', models.CharField(choices=[('player', 'Игрок'), ('referee', 'Судья'), ('coach', 'Тренер')], max_length=10, verbose_name='Роль')),
                ('sportmonks_id', models.CharField(blank=True, max_length=100, verbose_name='Sportmonks ID (снэпшот)')),
                ('current_first_name', models.CharField(blank=True, max_length=120, verbose_name='Текущее имя')),
                ('current_last_name', models.CharField(blank=True, max_length=120, verbose_name='Текущая фамилия')),
                ('suggested_first_name', models.CharField(blank=True, max_length=120, verbose_name='Предложенное имя')),
                ('suggested_last_name', models.CharField(blank=True, max_length=120, verbose_name='Предложенная фамилия')),
                ('confidence', models.CharField(blank=True, choices=[('high', 'Высокая'), ('medium', 'Средняя'), ('low', 'Низкая')], max_length=10, verbose_name='Уверенность')),
                ('reasoning', models.TextField(blank=True, verbose_name='Обоснование от Gemini')),
                ('matches_current', models.BooleanField(default=False, verbose_name='Gemini подтвердил текущее написание')),
                ('status', models.CharField(choices=[('pending_review', 'Ждёт проверки staff'), ('approved', 'Подтверждено'), ('rejected', 'Отклонено'), ('check_failed', 'Ошибка запроса к Gemini')], db_index=True, default='pending_review', max_length=20, verbose_name='Статус')),
                ('error_message', models.TextField(blank=True, verbose_name='Ошибка запроса')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True, verbose_name='Когда проверено')),
                ('content_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype', verbose_name='Тип сущности')),
                ('reviewed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_name_suggestions', to=settings.AUTH_USER_MODEL, verbose_name='Кто проверил')),
            ],
            options={
                'verbose_name': 'Предложение по ФИО (ИИ)',
                'verbose_name_plural': 'Предложения по ФИО (ИИ)',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='nameverificationsuggestion',
            index=models.Index(fields=['status', '-created_at'], name='name_suggestion_status_idx'),
        ),
        migrations.AddIndex(
            model_name='nameverificationsuggestion',
            index=models.Index(fields=['content_type', 'object_id'], name='name_suggestion_entity_idx'),
        ),
        migrations.AddConstraint(
            model_name='nameverificationsuggestion',
            constraint=models.UniqueConstraint(condition=models.Q(('status', 'pending_review')), fields=('content_type', 'object_id'), name='uniq_pending_review_per_entity'),
        ),
        migrations.CreateModel(
            name='ConfirmedNameCorrection',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('wrong_text', models.CharField(db_index=True, max_length=120, unique=True, verbose_name='Неверный текст')),
                ('correct_text', models.CharField(max_length=120, verbose_name='Верный текст')),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='Кто подтвердил')),
                ('source_suggestion', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='confirmed_corrections', to='parsers.nameverificationsuggestion', verbose_name='Из предложения ИИ')),
            ],
            options={
                'verbose_name': 'Подтверждённая поправка ФИО',
                'verbose_name_plural': 'Подтверждённые поправки ФИО',
                'ordering': ['wrong_text'],
            },
        ),
    ]
