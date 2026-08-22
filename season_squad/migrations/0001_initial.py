# season_squad/migrations/0001_initial.py
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('seasons', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SeasonBestXI',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('formation', models.CharField(default='4-3-3', max_length=20, verbose_name='Формация')),
                ('is_final', models.BooleanField(default=False, help_text='После включения recompute больше не трогает эту сборную — используется после закрытия сезона и последних голосований.', verbose_name='Зафиксирована как итоговая')),
                ('finalized_at', models.DateTimeField(blank=True, null=True, verbose_name='Зафиксирована')),
                ('last_computed_at', models.DateTimeField(blank=True, null=True, verbose_name='Последний пересчёт')),
                ('season', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='best_xi', to='seasons.season', verbose_name='Сезон')),
            ],
            options={
                'verbose_name': 'Живая сборная сезона',
                'verbose_name_plural': 'Живые сборные сезонов',
            },
        ),
        migrations.CreateModel(
            name='SeasonBestXISlot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('slot_code', models.CharField(max_length=10, verbose_name='Код слота')),
                ('order', models.PositiveSmallIntegerField(default=0, verbose_name='Порядок отображения')),
                ('object_id', models.UUIDField(blank=True, null=True)),
                ('occupant_name', models.CharField(blank=True, max_length=255, verbose_name='Имя')),
                ('occupant_team_name', models.CharField(blank=True, max_length=255, verbose_name='Клуб')),
                ('occupant_photo_url', models.CharField(blank=True, max_length=500, verbose_name='URL фото')),
                ('occupant_profile_url', models.CharField(blank=True, max_length=500, verbose_name='Ссылка на профиль')),
                ('season_score', models.FloatField(blank=True, null=True, verbose_name='Рейтинг сезона')),
                ('matches_count', models.PositiveIntegerField(default=0, verbose_name='Матчей')),
                ('votes_count', models.PositiveIntegerField(default=0, verbose_name='Голосов')),
                ('is_confident', models.BooleanField(default=False, verbose_name='Достаточно данных')),
                ('rank_change', models.CharField(choices=[('new', 'Вошёл в состав'), ('up', 'Поднялся'), ('down', 'Опустился'), ('same', 'Без изменений')], default='new', max_length=10, verbose_name='Изменение')),
                ('rank_change_delta', models.PositiveSmallIntegerField(blank=True, null=True, verbose_name='На сколько мест')),
                ('explanation', models.TextField(blank=True, verbose_name='Почему в XI')),
                ('content_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype')),
                ('best_xi', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slots', to='season_squad.seasonbestxi', verbose_name='Сборная')),
            ],
            options={
                'verbose_name': 'Слот сборной',
                'verbose_name_plural': 'Слоты сборной',
                'ordering': ['order'],
            },
        ),
        migrations.CreateModel(
            name='SeasonPositionRanking',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('slot_code', models.CharField(max_length=10, verbose_name='Код слота')),
                ('object_id', models.UUIDField()),
                ('rank', models.PositiveSmallIntegerField(verbose_name='Ранг в пуле')),
                ('season_score', models.FloatField(verbose_name='Рейтинг сезона')),
                ('matches_count', models.PositiveIntegerField(default=0, verbose_name='Матчей')),
                ('votes_count', models.PositiveIntegerField(default=0, verbose_name='Голосов')),
                ('computed_at', models.DateTimeField(verbose_name='Партия пересчёта')),
                ('content_type', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype')),
                ('best_xi', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rankings', to='season_squad.seasonbestxi', verbose_name='Сборная')),
            ],
            options={
                'verbose_name': 'Ранг кандидата в сборную',
                'verbose_name_plural': 'Ранги кандидатов в сборную',
                'ordering': ['slot_code', 'rank'],
            },
        ),
        migrations.AddConstraint(
            model_name='seasonbestxislot',
            constraint=models.UniqueConstraint(fields=('best_xi', 'slot_code'), name='unique_best_xi_slot'),
        ),
        migrations.AddIndex(
            model_name='seasonbestxislot',
            index=models.Index(fields=['content_type', 'object_id'], name='season_squ_content_e0f8c1_idx'),
        ),
        migrations.AddIndex(
            model_name='seasonpositionranking',
            index=models.Index(fields=['best_xi', 'slot_code', 'computed_at'], name='season_squ_best_xi_f3f6f9_idx'),
        ),
        migrations.AddIndex(
            model_name='seasonpositionranking',
            index=models.Index(fields=['best_xi', 'slot_code', 'rank'], name='season_squ_best_xi_6c2f7f_idx'),
        ),
        migrations.AddIndex(
            model_name='seasonpositionranking',
            index=models.Index(fields=['content_type', 'object_id'], name='season_squ_content_9b3f2a_idx'),
        ),
    ]
