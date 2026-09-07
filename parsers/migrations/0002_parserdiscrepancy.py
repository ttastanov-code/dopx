# Generated manually (см. комментарий в 0001_initial.py — makemigrations
# недоступен в этой песочнице без подключённой БД).
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0001_initial'),
        ('parsers', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ParserDiscrepancy',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('match_label', models.CharField(max_length=200, verbose_name='Матч (снэпшот)')),
                ('field_name', models.CharField(max_length=50, verbose_name='Поле')),
                ('old_value', models.CharField(max_length=200, verbose_name='Было')),
                ('new_value', models.CharField(max_length=200, verbose_name='Стало')),
                ('reviewed', models.BooleanField(db_index=True, default=False, verbose_name='Разобрано')),
                ('reviewed_at', models.DateTimeField(blank=True, null=True, verbose_name='Когда разобрано')),
                ('note', models.TextField(blank=True, verbose_name='Заметка')),
                ('match', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='parser_discrepancies', to='matches.match',
                    verbose_name='Матч',
                )),
                ('reviewed_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='+', to=settings.AUTH_USER_MODEL,
                    verbose_name='Кто разобрал',
                )),
            ],
            options={
                'verbose_name': 'Расхождение импорта',
                'verbose_name_plural': 'Расхождения импорта',
                'ordering': ['reviewed', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='parserdiscrepancy',
            index=models.Index(fields=['reviewed', '-created_at'], name='parser_discrepancy_review_idx'),
        ),
    ]
