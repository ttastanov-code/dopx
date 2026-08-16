# core/tasks.py
"""
Инфраструктурные периодические задачи, не привязанные к конкретному
бизнес-домену (в отличие от notifications/tasks.py, users/tasks.py и т.д.).

cleanup_expired_captchas: django-simple-captcha не чистит свою таблицу
captcha.CaptchaStore сама после CAPTCHA_TIMEOUT (dopx/settings.py) — растёт
на каждый показ формы регистрации. Прямой ORM-запрос вместо вызова
management-команды clean_captchas из Celery — без лишнего слоя косвенности.
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
