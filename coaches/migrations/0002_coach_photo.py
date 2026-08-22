# coaches/migrations/0002_coach_photo.py
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('coaches', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='coach',
            name='photo',
            field=models.ImageField(blank=True, null=True, upload_to='coaches/', verbose_name='Фото'),
        ),
    ]
