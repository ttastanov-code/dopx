# core/tasks.py
"""
НОВЫЙ ФАЙЛ. Инфраструктурные периодические задачи, не привязанные к
конкретному бизнес-домену (в отличие от `notifications/tasks.py`,
`users/tasks.py` и т.д. — там задачи про уведомления/достижения).

`cleanup_expired_captchas`: `django-simple-captcha` хранит каждый
сгенерированный челлендж отдельной строкой в своей таблице
`captcha.CaptchaStore` до истечения `CAPTCHA_TIMEOUT` минут (см.
`dopx/settings.py`), но НЕ удаляет истёкшие записи сама — без периодической
очистки таблица растёт бесконечно (каждый показ формы регистрации создаёт
новую строку, включая ботов и просто обновления страницы). Пакет поставляет
management-команду `clean_captchas`, но вызывать management-команду из
Celery — лишний слой косвенности; надёжнее и быстрее удалить просроченные
строки прямым ORM-запросом.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def cleanup_expired_captchas() -> int:
    """Удаляет просроченные записи `CaptchaStore`. Возвращает число удалённых строк."""
    from captcha.models import CaptchaStore

    deleted, _ = CaptchaStore.objects.filter(expiration__lt=timezone.now()).delete()
    if deleted:
        logger.info("Удалено %d просроченных captcha-записей.", deleted)
    return deleted
