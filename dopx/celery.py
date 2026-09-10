# dopx/celery.py
import os
from celery import Celery

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dopx.settings')

app = Celery('dopx')
app.config_from_object('django.conf:settings', namespace='CELERY')

# ИСПРАВЛЕНО (2026-09-10, продакшен-инцидент: воркер писал в лог "Received
# unregistered task of type 'parsers.sportmonks.tasks.sportmonks_update_live'"
# на КАЖДЫЙ тик Celery Beat, задача молча отбрасывалась). КОРНЕВАЯ ПРИЧИНА:
# `app.autodiscover_tasks()` без аргументов (в связке с django-celery) ищет
# модуль `tasks.py` ТОЛЬКО напрямую в пакете каждого приложения из
# INSTALLED_APPS — 'parsers' там есть, поэтому `parsers/tasks.py`
# импортируется и регистрируется исправно, а `parsers/sportmonks/tasks.py`
# — ВЛОЖЕННЫЙ подпакет ('parsers.sportmonks' САМ по себе не приложение в
# INSTALLED_APPS, см. dopx/settings.py) — автодискавери его никогда не
# видит. Ни одна из 5 задач CELERY_BEAT_SCHEDULE с task='parsers.sportmonks.
# tasks.*' (см. dopx/settings.py) никогда не регистрировалась в процессе
# воркера — а значит НИКОГДА реально не выполнялась ни по расписанию, ни по
# кнопке "Обновить live-матчи"/"Подтянуть составы" и т.д. в staff-панели
# (dashboard/parser_tools.py::trigger_task тоже идёт через .delay(), тот же
# путь и тот же тупик) — работал только ручной ресинк ОДНОГО матча
# (dashboard/parser_tools.py::resync_match), потому что он вызывает
# importers.import_full_fixture НАПРЯМУЮ, в процессе Django, без очереди.
# Это объясняет, почему фикс "вечного live" (parsers/sportmonks/tasks.py::
# sportmonks_update_live) не мог сработать автоматически ни разу — сама
# задача физически не запускалась. Явно указываем пакет `parsers.sportmonks`
# ВТОРЫМ вызовом — Celery ищет там `tasks.py` независимо от INSTALLED_APPS.
app.autodiscover_tasks()
app.autodiscover_tasks(['parsers.sportmonks'], related_name='tasks')

@app.task(bind=True)
def debug_task(self):
    print(f'Request: {self.request!r}')
    return 'OK'