# dopx/celery.py
import os
from celery import Celery
from celery.signals import task_postrun

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dopx.settings')

app = Celery('dopx')
app.config_from_object('django.conf:settings', namespace='CELERY')

# parsers.sportmonks — вложенный пакет, autodiscover его не видит; указываем явно.
app.autodiscover_tasks()
app.autodiscover_tasks(['parsers.sportmonks'], related_name='tasks')

@task_postrun.connect
def _heartbeat_on_task(sender=None, **kwargs):
    """Пульс по факту выполненной задачи: при длинной очереди воркер не выглядит мёртвым."""
    try:
        from core import heartbeat
        heartbeat.task_done(getattr(sender.request, 'hostname', None) or '')
    except Exception:
        pass


@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    return 'OK'