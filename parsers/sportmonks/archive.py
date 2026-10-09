# parsers/sportmonks/archive.py
"""Архив сырых ответов API на диске (data/api_archive): переимпорт и проигрывание live без доступа к API."""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def root() -> Path:
    return Path(settings.API_ARCHIVE_DIR)


def path_for(*parts: str) -> Path:
    return root().joinpath(*parts).with_suffix(".json.gz")


def save(payload, *parts: str) -> Path:
    path = path_for(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    tmp.replace(path)
    return path


def load(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def record_live(fixtures: list) -> None:
    """Снимок live-опроса (live/<дата>/<время>) — для повторного проигрывания матча. Ошибка записи не мешает опросу."""
    if not fixtures:
        return
    now = timezone.now()
    try:
        save({"at": now.isoformat(), "fixtures": fixtures}, "live", f"{now:%Y-%m-%d}", f"{now:%H%M%S}")
    except OSError as exc:
        logger.warning("api archive: live-снимок не записан: %s", exc)


def record_fixture(full: dict) -> None:
    """Полный ответ по матчу при live-догрузке (live/<дата>/fixture_<id>_<время>)."""
    if not full or full.get("id") is None:
        return
    now = timezone.now()
    try:
        save(full, "live", f"{now:%Y-%m-%d}", f"fixture_{full['id']}_{now:%H%M%S}")
    except OSError as exc:
        logger.warning("api archive: снимок матча не записан: %s", exc)
