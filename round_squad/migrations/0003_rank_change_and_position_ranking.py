# Generated manually (см. комментарий в других *_manually миграциях проекта —
# makemigrations недоступен в этой песочнице без подключённой БД).
#
# docs/adr/0032-squad-explainability-v2.md: "Изменение позиции" для тура —
# те же поля, что у SeasonBestXISlot (rank_change/rank_change_delta), плюс
# новая модель RoundPositionRanking (полный ранжированный снимок слота
# тура — источник для сравнения с прошлым туром и "ближайшим конкурентом").

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('round_squad', '0002_rename_round_squa_content_2f6a41_idx_round_squad_content_a5c212_idx'),
    ]

    operations = [
        migrations.AddField(
            model_name='roundbestxislot',
            name='rank_change',
            field=models.CharField(
                choices=[
                    ('new', 'Не играл в прошлом туре'), ('up', 'Поднялся'),
                    ('down', 'Опустился'), ('same', 'Без изменений'),
                ],
                default='new', max_length=10, verbose_name='Изменение',
            ),
        ),
        migrations.AddField(
            model_name='roundbestxislot',
            name='rank_change_delta',
            field=models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='На сколько мест'),
        ),
        migrations.CreateModel(
            name='RoundPositionRanking',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('slot_code', models.CharField(max_length=10, verbose_name='Код слота')),
                ('object_id', models.UUIDField()),
                ('rank', models.PositiveSmallIntegerField(verbose_name='Ранг в пуле')),
                ('round_score', models.FloatField(verbose_name='Рейтинг тура')),
                ('votes_count', models.PositiveIntegerField(default=0, verbose_name='Голосов')),
                ('content_type', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype',
                )),
                ('round_best_xi', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name='rankings',
                    to='round_squad.roundbestxi', verbose_name='Тур',
                )),
            ],
            options={
                'verbose_name': 'Ранг кандидата в тур',
                'verbose_name_plural': 'Ранги кандидатов в тур',
                'ordering': ['slot_code', 'rank'],
            },
        ),
        migrations.AddIndex(
            model_name='roundpositionranking',
            index=models.Index(fields=['round_best_xi', 'slot_code'], name='round_squad_round_b_9e1a53_idx'),
        ),
        migrations.AddIndex(
            model_name='roundpositionranking',
            index=models.Index(fields=['content_type', 'object_id'], name='round_squad_content_9f2b64_idx'),
        ),
        migrations.AddConstraint(
            model_name='roundpositionranking',
            constraint=models.UniqueConstraint(
                fields=('round_best_xi', 'slot_code', 'rank'), name='unique_round_position_ranking',
            ),
        ),
    ]
