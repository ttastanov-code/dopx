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
    # Очистка старых сессий каждый день в 3:00
    'cleanup-old-sessions-daily': {
        'task': 'aggregates.tasks.cleanup_old_sessions',
        'schedule': crontab(hour=3, minute=0),
    },
    # Обновление статусов матчей каждый час
    'update-match-statuses-hourly': {
        'task': 'matches.tasks.update_match_statuses',
        'schedule': crontab(minute=0),
    },
}

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    return 'OK'