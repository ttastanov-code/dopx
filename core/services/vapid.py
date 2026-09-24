# core/services/vapid.py
"""Генерация VAPID-ключей для Web Push. Используется командой setup_push_keys
и автоматически на post_migrate (core/apps.py).
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
    """Генерирует пару VAPID-ключей (EC P-256) и дописывает в .env.
    В VAPID_PRIVATE_KEY пишется сам ключ (raw base64url), не путь к файлу.

    :return: (приватный ключ, application server key для JS).
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

    # 32 байта private_value, base64url без паддинга.
    private_value = private_key.private_numbers().private_value
    raw_private_key = private_value.to_bytes(32, 'big')
    private_key_content = base64.urlsafe_b64encode(raw_private_key).rstrip(b'=').decode()

    _upsert_env(env_path, {
        'VAPID_PRIVATE_KEY': private_key_content,
        'VAPID_PUBLIC_KEY': application_server_key,
    })

    return private_key_content, application_server_key


def _upsert_env(env_path: Path, values: dict[str, str]) -> None:
    """Обновляет/добавляет переменные в .env, остальное не трогает."""
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
    """post_migrate: создаёт ключи, только если их ещё нет.
    После генерации нужен перезапуск сервера. Ошибки только логируются.
    """
    if vapid_keys_configured():
        return

    try:
        generate_and_persist_vapid_keys()
    except Exception:
        logger.exception("ensure_vapid_keys_on_startup: не удалось сгенерировать VAPID-ключи")
        return

    # В проде — warning, чтобы не потерялось: без перезапуска push не работает.
    log = logger.warning if getattr(settings, 'ENVIRONMENT', '') == 'production' else logger.info
    log(
        "VAPID-ключи для Web Push сгенерированы автоматически и записаны "
        "в .env. Перезапустите сервер, чтобы push-уведомления заработали."
    )
