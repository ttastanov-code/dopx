# dopx/__init__.py
# Это гарантирует, что Celery app загружается при старте Django
from .celery import app as celery_app

__all__ = ('celery_app',)