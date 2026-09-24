# dopx/celery.py
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dopx.settings')

app = Celery('dopx')
app.config_from_object('django.conf:settings', namespace='CELERY')

# parsers.sportmonks — вложенный пакет, autodiscover его не видит; указываем явно.
app.autodiscover_tasks()
app.autodiscover_tasks(['parsers.sportmonks'], related_name='tasks')

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    return 'OK'