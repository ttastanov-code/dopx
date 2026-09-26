# core/management/seed_guard.py
"""Запрет сид/симуляционных команд там, где они испортят боевые рейтинги."""
from django.conf import settings
from django.core.management.base import CommandError


def ensure_seed_allowed() -> None:
    """Бросает CommandError, если ALLOW_SEED_COMMANDS выключен (по умолчанию — в проде)."""
    if not getattr(settings, "ALLOW_SEED_COMMANDS", False):
        raise CommandError(
            "Команда создаёт синтетические данные и отключена в этом окружении "
            "(ALLOW_SEED_COMMANDS=False)."
        )
