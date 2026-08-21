# Generated manually — см. analytics/migrations/0001_initial.py для
# объяснения, почему миграции в этом проекте написаны руками (нет
# сетевого доступа к PyPI в песочнице разработки). Поля 1:1 совпадают с
# partners/models.py. Перед мёржем в основную ветку прогнать
# `python manage.py makemigrations --check` на машине с рабочим окружением.
import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name='Partner',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=150, verbose_name='Название')),
                ('slug', models.SlugField(help_text='Используется в реферальной ссылке /go/<slug>/', max_length=60, unique=True, verbose_name='Слаг')),
                ('partner_type', models.CharField(choices=[
                    ('media', 'Спортивное медиа'),
                    ('club', 'Клубный паблик'),
                    ('influencer', 'Микроинфлюенсер'),
                    ('bookmaker', 'Букмекер'),
                    ('other', 'Другое'),
                ], default='other', max_length=20, verbose_name='Тип')),
                ('contact_name', models.CharField(blank=True, max_length=150, verbose_name='Контактное лицо')),
                ('contact_email', models.EmailField(blank=True, max_length=254, verbose_name='Email')),
                ('website', models.URLField(blank=True, verbose_name='Сайт')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активен')),
                ('notes', models.TextField(blank=True, help_text='Внутренние заметки по договорённости — не показываются публично', verbose_name='Заметки')),
            ],
            options={
                'verbose_name': 'Партнёр',
                'verbose_name_plural': 'Партнёры',
                'ordering': ['name'],
            },
        ),
        migrations.CreateModel(
            name='Banner',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('zone', models.CharField(choices=[
                    ('home_hero', 'Главная — верх'),
                    ('sidebar', 'Боковая колонка'),
                    ('match_detail', 'Страница матча'),
                    ('leaderboard', 'Лидерборд'),
                ], db_index=True, max_length=20, verbose_name='Зона показа')),
                ('title', models.CharField(help_text='Внутреннее название + alt-текст картинки, пользователю не показывается отдельно', max_length=150, verbose_name='Название')),
                ('image', models.ImageField(upload_to='banners/%Y/%m/', verbose_name='Изображение')),
                ('target_url', models.URLField(verbose_name='Ссылка перехода')),
                ('is_active', models.BooleanField(default=True, verbose_name='Активен')),
                ('starts_at', models.DateTimeField(blank=True, null=True, verbose_name='Показывать с')),
                ('ends_at', models.DateTimeField(blank=True, null=True, verbose_name='Показывать до')),
                ('priority', models.PositiveIntegerField(default=0, help_text='Выше число — чаще показывается среди активных баннеров той же зоны', verbose_name='Приоритет')),
                ('requires_age_disclaimer', models.BooleanField(default=False, help_text='Обязательно для букмекеров/гэмблинга — под баннером покажется дисклеймер', verbose_name='Требует пометки 18+')),
                ('partner', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='banners',
                    to='partners.partner',
                    help_text='Необязательно — баннер может быть собственным промо DOPX без привязки к партнёру',
                    verbose_name='Партнёр',
                )),
            ],
            options={
                'verbose_name': 'Баннер',
                'verbose_name_plural': 'Баннеры',
                'ordering': ['-priority', '-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='banner',
            index=models.Index(fields=['zone', 'is_active'], name='partners_ba_zone_ac_idx'),
        ),
    ]
