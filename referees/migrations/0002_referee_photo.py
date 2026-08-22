# referees/migrations/0002_referee_photo.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('referees', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='referee',
            name='photo',
            field=models.ImageField(blank=True, null=True, upload_to='referees/', verbose_name='Фото'),
        ),
    ]
