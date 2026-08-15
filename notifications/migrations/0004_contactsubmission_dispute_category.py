from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0003_notification_email_sent_at_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='contactsubmission',
            name='category',
            field=models.CharField(
                choices=[
                    ('general', 'Общий вопрос'),
                    ('bug', 'Сообщение об ошибке'),
                    ('feature', 'Предложение функции'),
                    ('evaluation', 'Проблема с оценкой матча'),
                    ('account', 'Вопрос по аккаунту'),
                    ('dispute', 'Оспорить рейтинг / право на ответ'),
                    ('other', 'Другое'),
                ],
                default='general',
                max_length=30,
                verbose_name='Категория',
            ),
        ),
    ]
