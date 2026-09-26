# notifications/services.py
"""Web Push."""
from __future__ import annotations

import json
import logging
from typing import Iterable

from django.conf import settings

logger = logging.getLogger(__name__)

# Таймаут HTTP-запроса к push-сервису, сек.
PUSH_HTTP_TIMEOUT = 10

# TTL и срочность по типам. TTL=0 (дефолт pywebpush) = «доставить сейчас или выбросить»:
# спящий телефон такой пуш теряет. urgency=high — будит устройство сразу, иначе
# Android копит пуши и отдаёт пачкой.
PUSH_PROFILES = {
    'match_event': {'ttl': 15 * 60, 'urgency': 'high'},
    'match_started': {'ttl': 30 * 60, 'urgency': 'high'},
    'lineups_available': {'ttl': 2 * 60 * 60, 'urgency': 'normal'},
    'prediction_closing': {'ttl': 60 * 60, 'urgency': 'normal'},
    'voting_open': {'ttl': 24 * 60 * 60, 'urgency': 'normal'},
    'match_finished': {'ttl': 6 * 60 * 60, 'urgency': 'high'},
    'evaluation_reminder': {'ttl': 60 * 60, 'urgency': 'high'},
    'prediction_result': {'ttl': 12 * 60 * 60, 'urgency': 'normal'},
    'achievement': {'ttl': 24 * 60 * 60, 'urgency': 'normal'},
    'round_results': {'ttl': 24 * 60 * 60, 'urgency': 'normal'},
    'match_changed': {'ttl': 12 * 60 * 60, 'urgency': 'normal'},
    'voting_closing': {'ttl': 60 * 60, 'urgency': 'high'},
    'ratings_published': {'ttl': 24 * 60 * 60, 'urgency': 'normal'},
    'default': {'ttl': 6 * 60 * 60, 'urgency': 'normal'},
}

# kind -> ключ пользовательской настройки. Нет в словаре (default, тест) — шлём всегда.
PUSH_KIND_SETTING = {
    'match_event': 'push_live',
    'match_started': 'push_live',
    'lineups_available': 'push_lineups',
    'voting_open': 'push_voting',
    'match_finished': 'push_voting',
    'evaluation_reminder': 'push_voting',
    'prediction_closing': 'push_predictions',
    'prediction_result': 'push_predictions',
    'achievement': 'push_achievements',
    'round_results': 'push_round_results',
    'match_changed': 'push_match_changes',
    'voting_closing': 'push_voting',
    'ratings_published': 'push_results',
}


def _users_allowing(user_ids: list, kind: str) -> list:
    """Оставляет пользователей, у которых push этого типа не выключен."""
    setting_key = PUSH_KIND_SETTING.get(kind)
    if not setting_key or not user_ids:
        return user_ids

    from users.models import User

    users = User.objects.filter(id__in=user_ids).only('id', '_notification_settings')
    return [u.id for u in users if u.get_notification_setting(setting_key, True)]


def _push_ready() -> bool:
    if not settings.VAPID_PRIVATE_KEY or not settings.VAPID_PUBLIC_KEY:
        logger.debug("push: VAPID-ключи не настроены, пропуск")
        return False
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        logger.warning("push: пакет pywebpush не установлен")
        return False
    return True


def send_push_to_users(
    user_ids: Iterable,
    *,
    title: str,
    body: str,
    url: str = '/',
    kind: str = 'default',
    tag: str | None = None,
) -> int:
    """Push всем подпискам указанных пользователей. Исключения наружу не бросает.

    kind — ключ PUSH_PROFILES (TTL и срочность) и PUSH_KIND_SETTING (выключенные
    пользователем типы отсекаются). tag — одинаковый tag заменяет
    предыдущее уведомление на устройстве вместо новой строки.
    Возвращает число успешных отправок.
    """
    if not _push_ready():
        return 0

    import requests
    from pywebpush import WebPushException, webpush

    from users.models import PushSubscription

    allowed_ids = _users_allowing(list(user_ids), kind)
    if not allowed_ids:
        return 0
    subscriptions = list(PushSubscription.objects.filter(user_id__in=allowed_ids))
    if not subscriptions:
        return 0

    profile = PUSH_PROFILES.get(kind, PUSH_PROFILES['default'])
    payload = json.dumps({'title': title, 'body': body, 'url': url, 'tag': tag})
    headers = {'Urgency': profile['urgency']}
    sent = 0
    stale_ids = []

    # Одна сессия на всю рассылку — без нового TLS-рукопожатия на каждую подписку.
    with requests.Session() as session:
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
                    ttl=profile['ttl'],
                    headers=headers,
                    timeout=PUSH_HTTP_TIMEOUT,
                    requests_session=session,
                )
                sent += 1
            except WebPushException as exc:
                status_code = getattr(exc.response, 'status_code', None)
                if status_code in (404, 410):
                    stale_ids.append(sub.id)
                else:
                    logger.warning(f"push: не отправлен для подписки {sub.id}: {exc}")
            except Exception as exc:
                logger.warning(f"push: ошибка для подписки {sub.id}: {exc}")

    if stale_ids:
        PushSubscription.objects.filter(id__in=stale_ids).delete()
    return sent


def send_push_to_user(user, *, title: str, body: str, url: str = '/', kind: str = 'default', tag: str | None = None) -> int:
    """Push на все подписки одного пользователя."""
    return send_push_to_users([user.id], title=title, body=body, url=url, kind=kind, tag=tag)
