# core/services/vapid.py
"""
Продуктовый аудит, раздел 5c ("PWA + Web Push") — общая логика генерации
VAPID-ключей, переиспользуемая и командой `manage.py setup_push_keys`
(явный ручной запуск, например с `--force` для перевыпуска), и сигналом
`post_migrate` (см. `core/apps.py::CoreConfig.ready()`) — автоматический
запуск на КАЖДОМ `python manage.py migrate`, чтобы пользователю не нужно
было отдельно помнить о существовании этой команды при разворачивании
проекта на новом окружении (через год, на новом сервере — где угодно).
`migrate` в любом случае обязателен при первом деплое, значит и генерация
ключей произойдёт сама собой, без дополнительного шага в голове.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.conf import settings

logger = logging.getLogger(__name__)


def vapid_keys_configured() -> bool:
    return bool(settings.VAPID_PRIVATE_KEY and settings.VAPID_PUBLIC_KEY)


def generate_and_persist_vapid_keys() -> tuple[str, str]:
    """
    Генерирует пару VAPID-ключей (EC P-256, как того требует RFC 8292) и
    дописывает их в `.env`. НЕ проверяет, настроены ли ключи уже — это
    ответственность вызывающего кода (обе точки входа, `setup_push_keys`
    и `ensure_vapid_keys_on_startup`, сами решают, когда вызывать).

    :return: (путь к private_key.pem, application server key для JS).
    """
    env_path = Path(settings.BASE_DIR) / '.env'
    key_dir = Path(settings.BASE_DIR) / '.vapid'
    key_dir.mkdir(exist_ok=True)
    private_key_path = key_dir / 'private_key.pem'

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    private_key_path.write_bytes(private_pem)

    public_point = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    application_server_key = base64.urlsafe_b64encode(public_point).rstrip(b'=').decode()

    _upsert_env(env_path, {
        'VAPID_PRIVATE_KEY': str(private_key_path),
        'VAPID_PUBLIC_KEY': application_server_key,
    })

    return str(private_key_path), application_server_key


def _upsert_env(env_path: Path, values: dict[str, str]) -> None:
    """Точечно обновляет/добавляет переменные в .env, не трогая остальные строки."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    existing_keys = {
        line.split('=', 1)[0] for line in lines
        if '=' in line and not line.strip().startswith('#')
    }

    for key, value in values.items():
        new_line = f"{key}={value}"
        if key in existing_keys:
            lines = [new_line if line.startswith(f"{key}=") else line for line in lines]
        else:
            lines.append(new_line)

    env_path.write_text('\n'.join(lines) + '\n')


def ensure_vapid_keys_on_startup(**kwargs) -> None:
    """
    Receiver для сигнала `django.db.models.signals.post_migrate`
    (подключается в `core/apps.py::CoreConfig.ready()`, `sender=self` —
    фильтр, чтобы сработать РОВНО ОДИН РАЗ за прогон `migrate`, а не по
    разу на каждое из ~20 установленных приложений).

    Не делает НИЧЕГО, если ключи уже настроены — безопасно вызывается на
    каждом обычном деплое/`migrate` без риска перевыпустить ключи и
    сломать существующие push-подписки пользователей (`users.
    PushSubscription`) у уже работающего проекта.

    Пишет только в `.env` — уже запущенный процесс `migrate` не подхватит
    новые переменные окружения на лету (Python читает `os.environ` один
    раз при старте интерпретатора), поэтому лог ниже прямо просит
    перезапустить сервер. Ошибки генерации (например, нет прав на запись
    в `.env`) логируются и проглатываются — `migrate` не должен падать
    из-за побочной фичи Web Push.
    """
    if vapid_keys_configured():
        return

    try:
        private_key_path, _ = generate_and_persist_vapid_keys()
    except Exception:
        logger.exception("ensure_vapid_keys_on_startup: не удалось сгенерировать VAPID-ключи")
        return

    logger.info(
        "VAPID-ключи для Web Push сгенерированы автоматически (%s) и "
        "записаны в .env. Перезапустите сервер, чтобы push-уведомления "
        "заработали.",
        private_key_path,
    )
