# dashboard/access.py
"""
Проверка доступа к разделам дашборда (раздел «Роли доступа», 2026-09-23) —
см. dashboard/models.py::StaffAccessGrant/DASHBOARD_SECTIONS для полного
контекста безопасного дефолта (grandfather-правило).

Два потребителя одной и той же функции user_can_access_section():
  1. dashboard/middleware.py::DashboardSectionAccessMiddleware — жёсткая
     проверка на уровне HTTP (403, если запрещено) — реальная граница
     безопасности, её не обойти прямым запросом к URL.
  2. dashboard/templatetags/dashboard_extras.py::can_access_section —
     та же функция в шаблоне _nav.html, чтобы не показывать ссылки на
     разделы, куда всё равно не пустят (UX, не граница безопасности сама
     по себе).
"""
from __future__ import annotations

from .models import StaffAccessGrant

# Порядок ВАЖЕН: префиксы проверяются по очереди, первый совпавший
# побеждает — более специфичные пути (/staff/dashboard/partners/,
# /staff/dashboard/banners/) идут ПЕРЕД корнем "/staff/dashboard/",
# иначе всё бы резолвилось в "overview".
SECTION_PATH_MAP: list[tuple[str, str]] = [
    ("/staff/dashboard/security/", ""),  # 2FA setup/challenge — всегда доступно, см. middleware EXEMPT ниже
    ("/staff/dashboard/access/", "access_roles"),
    ("/staff/dashboard/traffic/", "traffic"),
    ("/staff/dashboard/matches/", "matches"),
    ("/staff/dashboard/data-health/", "data_health"),
    ("/staff/dashboard/data-trust/", "data_trust"),
    ("/staff/dashboard/duplicate-players/", "duplicate_players"),
    ("/staff/dashboard/names-review/", "names_review"),
    ("/staff/dashboard/evaluations/", "evaluation_sessions"),
    ("/staff/dashboard/users/", "users"),
    ("/staff/dashboard/antifraud/", "antifraud"),
    ("/staff/dashboard/parser/", "parser_tools"),
    # partners/banners — подраздел «Реклама», см. докстринг partners_list()
    ("/staff/dashboard/partners/", "ads"),
    ("/staff/dashboard/banners/", "ads"),
    ("/staff/dashboard/ads/", "ads"),
    ("/staff/dashboard/audit/", "audit"),
    ("/staff/dashboard/announcements/", "announcements"),
    ("/staff/dashboard/settings/", "platform_settings"),
    ("/staff/dashboard/system-status/", "system_status"),
    ("/staff/dashboard/scripts/", "scripts"),
]

# "access_roles" — управление ЧУЖИМИ правами доступа. Сознательно ЖЁСТКО
# запрещено проверять через StaffAccessGrant (см. user_can_access_section
# ниже) — иначе кто-то мог бы выдать сам себе доступ к разделу, который
# выдаёт доступ, классическая дыра privilege escalation. Только is_superuser.
SUPERUSER_ONLY_SECTIONS = {"access_roles"}


def resolve_section_for_path(path: str) -> str | None:
    """Возвращает ключ раздела для пути, "" если путь всегда разрешён
    (2FA-подсистема), None если путь вне /staff/dashboard/ вообще (общий
    корень тоже не входит ни в один префикс — резолвится в "overview" через
    fallback ниже)."""
    for prefix, section in SECTION_PATH_MAP:
        if path.startswith(prefix):
            return section
    if path.startswith("/staff/dashboard/"):
        return "overview"
    return None


def user_can_access_section(user, section_key: str) -> bool:
    if not section_key:
        return True
    if getattr(user, "is_superuser", False):
        return True
    if section_key in SUPERUSER_ONLY_SECTIONS:
        return False
    try:
        grant = user.dashboard_access_grant
    except StaffAccessGrant.DoesNotExist:
        # Grandfather-правило — см. докстринг StaffAccessGrant в models.py.
        return True
    return grant.has_section(section_key)
