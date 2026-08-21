# Generated manually — см. 0001_initial.py.
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('partners', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='partner',
            name='feed_token',
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True, verbose_name='Токен контент-фида'),
        ),
    ]
