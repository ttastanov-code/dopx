# core/uploads.py
"""Проверка пользовательских вложений: только картинки и PDF, имя — случайное."""
from __future__ import annotations

import uuid

from PIL import Image, UnidentifiedImageError

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024

# Формат Pillow -> расширение.
ALLOWED_IMAGE_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp", "GIF": "gif"}


class AttachmentRejected(Exception):
    """Файл не прошёл проверку; текст — для пользователя."""


def validate_attachment(uploaded) -> str:
    """Проверяет содержимое файла (а не имя) и возвращает безопасное имя для сохранения."""
    if uploaded.size > MAX_ATTACHMENT_BYTES:
        raise AttachmentRejected("Файл слишком большой. Максимум 5 МБ.")

    uploaded.seek(0)
    head = uploaded.read(5)
    uploaded.seek(0)
    if head == b"%PDF-":
        return f"{uuid.uuid4().hex}.pdf"

    try:
        with Image.open(uploaded) as img:
            fmt = img.format
            img.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        raise AttachmentRejected("Можно прикрепить только изображение (PNG, JPG, WEBP, GIF) или PDF.")
    finally:
        uploaded.seek(0)

    ext = ALLOWED_IMAGE_FORMATS.get(fmt)
    if ext is None:
        raise AttachmentRejected("Можно прикрепить только изображение (PNG, JPG, WEBP, GIF) или PDF.")
    return f"{uuid.uuid4().hex}.{ext}"
