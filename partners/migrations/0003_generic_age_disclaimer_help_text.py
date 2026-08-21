# Generated manually — см. 0001_initial.py.
# Меняется только help_text (не влияет на схему БД) — формулировка поля
# requires_age_disclaimer была жёстко привязана к букмекерам/гэмблингу,
# хотя пометка 18+ нужна для любого возрастного контента (алкоголь, табак
# и т.п.). Миграция нужна только для консистентности состояния моделей.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('partners', '0002_partner_feed_token'),
    ]

    operations = [
        migrations.AlterField(
            model_name='banner',
            name='requires_age_disclaimer',
            field=models.BooleanField(
                default=False,
                help_text='Для любого контента 18+ (букмекеры/гэмблинг, алкоголь, табак и т.п.) — под баннером покажется дисклеймер',
                verbose_name='Требует пометки 18+',
            ),
        ),
    ]
