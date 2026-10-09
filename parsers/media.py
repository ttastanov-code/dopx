# parsers/media.py
"""Фото и гербы с CDN поставщика — в своё хранилище (photo/logo в приоритете над *_url).
Заглушки поставщика («NO PHOTO YET», серый силуэт) не сохраняются, их ссылка стирается."""
from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from django.core.files.base import ContentFile
from django.db.models import Q

logger = logging.getLogger(__name__)

IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg"}

# md5 известных заглушек поставщика (URL у них как у обычного фото игрока).
PLACEHOLDER_MD5 = frozenset({
    "0b8948fad10a299bbc6fbf5d3c3518bc",  # NO PHOTO YET
    "f512b984f93ca6915dd623351b93b531",  # NO PHOTO YET, другой вариант
    "430d67fd79ad0a355b212d5780886e34",  # серый силуэт в круге
})


def entities():
    """Модель -> (поле файла, поле ссылки)."""
    from coaches.models import Coach
    from players.models import Player
    from referees.models import Referee
    from teams.models import Team

    return {
        "players": (Player, "photo", "photo_url"),
        "coaches": (Coach, "photo", "photo_url"),
        "referees": (Referee, "photo", "photo_url"),
        "teams": (Team, "logo", "logo_url"),
    }


def is_placeholder(content: bytes) -> bool:
    return hashlib.md5(content).hexdigest() in PLACEHOLDER_MD5


def fetch(url: str) -> tuple[bytes, str] | None:
    """Картинка и расширение; не картинка или ошибка — None."""
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
    ext = IMAGE_TYPES.get(ctype) or os.path.splitext(urlparse(url).path)[1].lower()
    if not resp.content or ext not in IMAGE_TYPES.values():
        return None
    return resp.content, ext


def pending(name: str, refresh: bool = False):
    """Объекты со ссылкой, у которых ещё нет своего файла (или все — при refresh)."""
    model, file_field, url_field = entities()[name]
    qs = model.objects.exclude(**{f"{url_field}__isnull": True}).exclude(**{url_field: ""})
    if not refresh:
        qs = qs.filter(Q(**{file_field: ""}) | Q(**{f"{file_field}__isnull": True}))
    return qs


def localize(name: str, refresh: bool = False, workers: int = 8) -> dict:
    """Скачивает недостающее. Итог: {'saved', 'placeholders', 'failed'}."""
    _, file_field, url_field = entities()[name]
    todo = list(pending(name, refresh))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda obj: (obj, fetch(getattr(obj, url_field))), todo))
    stats = {"saved": 0, "placeholders": 0, "failed": 0}
    for obj, got in results:
        if not got:
            stats["failed"] += 1
            continue
        content, ext = got
        if is_placeholder(content):
            # Заглушка — не фото: ссылку стираем, на сайте будут инициалы.
            setattr(obj, url_field, "")
            obj.save(update_fields=[url_field])
            stats["placeholders"] += 1
            continue
        key = getattr(obj, "sportmonks_id", None) or obj.pk
        getattr(obj, file_field).save(f"sm_{key}{ext}", ContentFile(content), save=False)
        obj.save(update_fields=[file_field])
        stats["saved"] += 1
    return stats


def purge_placeholders() -> int:
    """Убирает уже скачанные заглушки: файл и ссылку."""
    removed = 0
    for model, file_field, url_field in entities().values():
        for obj in model.objects.exclude(**{file_field: ""}).exclude(**{f"{file_field}__isnull": True}):
            stored = getattr(obj, file_field)
            try:
                with stored.open("rb") as fh:
                    placeholder = is_placeholder(fh.read())
            except OSError:
                continue
            if placeholder:
                stored.delete(save=False)
                setattr(obj, url_field, "")
                obj.save(update_fields=[file_field, url_field])
                removed += 1
    return removed
