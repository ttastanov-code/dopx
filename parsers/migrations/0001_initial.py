# Generated manually (parsers app had no models until ParserSyncRun —
# писать вручную, т.к. makemigrations в этой песочнице без доступа к БД).
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='ParserSyncRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('task_name', models.CharField(db_index=True, max_length=100, verbose_name='Задача')),
                ('started_at', models.DateTimeField(verbose_name='Начало')),
                ('total', models.PositiveIntegerField(default=0, verbose_name='Всего матчей')),
                ('updated', models.PositiveIntegerField(default=0, verbose_name='Обновлено')),
                ('unchanged', models.PositiveIntegerField(default=0, verbose_name='Без изменений')),
                ('errors', models.PositiveIntegerField(default=0, verbose_name='Ошибок')),
                ('new_events', models.PositiveIntegerField(default=0, verbose_name='Новых событий')),
                ('status_changes', models.PositiveIntegerField(default=0, verbose_name='Смен статуса')),
                ('skipped_locked', models.PositiveIntegerField(default=0, verbose_name='Пропущено (лок)')),
                ('error_samples', models.JSONField(blank=True, default=list, verbose_name='Сэмплы ошибок')),
            ],
            options={
                'verbose_name': 'Запуск синхронизации',
                'verbose_name_plural': 'Запуски синхронизации',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='parsersyncrun',
            index=models.Index(fields=['task_name', '-created_at'], name='parser_run_task_time_idx'),
        ),
    ]
