# notifications/migrations/0009_contactsubmission_data_error_category.py
# Центр доверия к данным (2026-09-09): категория 'data_error' + опциональная
# привязка обращения к матчу (see notifications/models.py::ContactSubmission).
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('matches', '0001_initial'),
        ('notifications', '0008_alter_notification_notification_type'),
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
                    ('data_error', 'Ошибка в данных матча'),
                    ('other', 'Другое'),
                ],
                default='general',
                max_length=30,
                verbose_name='Категория',
            ),
        ),
        migrations.AddField(
            model_name='contactsubmission',
            name='related_match',
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='data_error_reports', to='matches.match',
                verbose_name='Матч (если жалоба на данные)',
            ),
        ),
    ]
