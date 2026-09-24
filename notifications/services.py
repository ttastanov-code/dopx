# notifications/services.py
"""Web Push."""
from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def send_push_to_user(user, *, title: str, body: str, url: str = '/') -> int:
    """Push на все подписки пользователя. Исключения наружу не бросает.
    Возвращает число успешных отправок.
    """
    if not settings.VAPID_PRIVATE_KEY or not settings.VAPID_PUBLIC_KEY:
        logger.debug("send_push_to_user: VAPID-ключи не настроены, пропуск (see VAPID_PUBLIC_KEY в settings.py)")
        return 0

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("send_push_to_user: пакет pywebpush не установлен (см. requirements.txt)")
        return 0

    from users.models import PushSubscription

    subscriptions = list(PushSubscription.objects.filter(user=user))
    if not subscriptions:
        return 0

    payload = json.dumps({'title': title, 'body': body, 'url': url})
    sent = 0
    stale_ids = []

    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=settings.VAPID_PRIVATE_KEY,
                vapid_claims={'sub': f'mailto:{settings.VAPID_ADMIN_EMAIL}'},
            )
            sent += 1
        except WebPushException as exc:
            status_code = getattr(exc.response, 'status_code', None)
            if status_code in (404, 410):
                # 404/410 — подписка мертва, удаляем.
                stale_ids.append(sub.id)
            else:
                logger.warning(f"send_push_to_user: push failed for subscription {sub.id}: {exc}")

    if stale_ids:
        PushSubscription.objects.filter(id__in=stale_ids).delete()

    return sent
