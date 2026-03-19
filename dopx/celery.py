# dopx/celery.py
import os
from celery import Celery
from celery.schedules import crontab

# Устанавливаем переменную окружения для Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dopx.settings')

# Создаём экземпляр Celery
app = Celery('dopx')

# Загружаем конфигурацию из Django settings
app.config_from_object('django.conf:settings', namespace='CELERY')

# Автообнаружение задач во всех приложениях
app.autodiscover_tasks()

# Настройка расписания периодических задач
app.conf.beat_schedule = {
    # Пересчёт всех агрегатов каждые 10 минут
    'recalculate-all-aggregates-every-10-minutes': {
        'task': 'aggregates.tasks.recalculate_all_aggregates',
        'schedule': crontab(minute='*/10'),
    },
    # Синхронизация KFF каждые 30 минут
    'sync-kff-recent-matches': {
        'task': 'parsers.tasks.sync_recent_matches',
        'schedule': crontab(minute='*/30'),
    },
    # Обновление статусов матчей каждый час
    'update-match-statuses-hourly': {
        'task': 'parsers.tasks.update_match_statuses',
        'schedule': crontab(minute=0),
    },
    # Очистка уведомлений ежедневно в 3:00
    'cleanup-old-sessions-daily': {
        'task': 'notifications.tasks.cleanup_old_notifications',
        'schedule': crontab(hour=3, minute=0),
    },
    # Напоминания о голосовании каждые 6 часов
    'voting-reminders': {
        'task': 'notifications.tasks.send_match_finished_notifications',
        'schedule': crontab(hour='*/6'),
    },
}

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    return 'OK'