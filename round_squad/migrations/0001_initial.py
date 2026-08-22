import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('seasons', '0001_initial'),
        ('matches', '0005_match_tour'),
    ]

    operations = [
        migrations.CreateModel(
            name='RoundBestXI',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('tour', models.PositiveSmallIntegerField(verbose_name='Тур')),
                ('formation', models.CharField(default='4-3-3', max_length=20, verbose_name='Формация')),
                ('is_final', models.BooleanField(default=False, help_text='Взводится автоматически, когда голосование по всем матчам тура закрыто (см. докстринг модели) — не требует ручного действия стаффа.', verbose_name='Зафиксирован')),
                ('finalized_at', models.DateTimeField(blank=True, null=True, verbose_name='Зафиксирован')),
                ('last_computed_at', models.DateTimeField(blank=True, null=True, verbose_name='Последний пересчёт')),
                ('player_of_round_object_id', models.UUIDField(blank=True, null=True)),
                ('player_of_round_name', models.CharField(blank=True, max_length=255, verbose_name='Игрок тура')),
                ('player_of_round_team_name', models.CharField(blank=True, max_length=255, verbose_name='Клуб')),
                ('player_of_round_photo_url', models.CharField(blank=True, max_length=500, verbose_name='URL фото')),
                ('player_of_round_profile_url', models.CharField(blank=True, max_length=500, verbose_name='Ссылка на профиль')),
                ('player_of_round_score', models.FloatField(blank=True, null=True, verbose_name='Рейтинг тура')),
                ('player_of_round_votes', models.PositiveIntegerField(default=0, verbose_name='Голосов')),
                ('player_of_round_explanation', models.TextField(blank=True, verbose_name='Почему игрок тура')),
                ('most_dramatic_match_score', models.FloatField(blank=True, null=True, verbose_name='Индекс драмы')),
                ('most_dramatic_match_explanation', models.TextField(blank=True, verbose_name='Почему этот матч')),
                ('share_card_path', models.CharField(blank=True, max_length=255, verbose_name='Путь к share-карточке')),
                ('most_dramatic_match', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='matches.match', verbose_name='Самый драматичный матч')),
                ('player_of_round_content_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='contenttypes.contenttype')),
                ('season', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='round_squads', to='seasons.season', verbose_name='Сезон')),
            ],
            options={
                'verbose_name': 'DOPX Лучшие тура',
                'verbose_name_plural': 'DOPX Лучшие тура',
                'ordering': ['-season__year', '-tour'],
            },
        ),
        migrations.CreateModel(
            name='RoundBestXISlot',
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
                ('round_score', models.FloatField(blank=True, null=True, verbose_name='Рейтинг тура')),
                ('votes_count', models.PositiveIntegerField(default=0, verbose_name='Голосов')),
                ('is_confident', models.BooleanField(default=False, verbose_name='Достаточно данных')),
                ('explanation', models.TextField(blank=True, verbose_name='Почему в составе тура')),
                ('content_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='contenttypes.contenttype')),
                ('round_best_xi', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slots', to='round_squad.roundbestxi', verbose_name='Тур')),
            ],
            options={
                'verbose_name': 'Слот тура',
                'verbose_name_plural': 'Слоты тура',
                'ordering': ['order'],
            },
        ),
        migrations.AddConstraint(
            model_name='roundbestxi',
            constraint=models.UniqueConstraint(fields=('season', 'tour'), name='unique_round_best_xi'),
        ),
        migrations.AddConstraint(
            model_name='roundbestxislot',
            constraint=models.UniqueConstraint(fields=('round_best_xi', 'slot_code'), name='unique_round_best_xi_slot'),
        ),
        migrations.AddIndex(
            model_name='roundbestxislot',
            index=models.Index(fields=['content_type', 'object_id'], name='round_squa_content_2f6a41_idx'),
        ),
    ]
