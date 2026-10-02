# dashboard/roles.py
"""Роли: группа + уровни по разделам (off/view/work/full, у страниц — off/on).
Уровни открывают разделы и выставляют группе права на модели."""
from __future__ import annotations

from django.contrib.auth.models import Group, Permission
from django.db import transaction

# Модели, с которыми работает раздел (app_label.model).
SECTION_MODELS: dict[str, list[str]] = {
    "matches": ["matches.match"],
    "data_health": ["matches.match"],
    "data_trust": ["notifications.contactsubmission", "parsers.parserdiscrepancy"],
    "duplicate_players": ["players.potentialduplicateplayer", "players.player"],
    "names_review": ["parsers.nameverificationsuggestion", "parsers.confirmednamecorrection"],
    "evaluation_sessions": ["evaluations.evaluationsession"],
    "users": ["users.user"],
    "antifraud": ["users.suspiciousactivityflag"],
    "ads": ["partners.partner", "partners.banner"],
    "announcements": ["notifications.notification"],
    "mourning": ["core.mourningmode"],
    "experts": ["engagement.expert", "engagement.experttake", "engagement.expertinvite"],
    # «Полный» — ещё и режимы автопостинга.
    "channel": ["adminbot.channelpost"],
    "platform_settings": ["core.platformsetting"],
    # Переключатель синхронизации Sportmonks хранится в настройках платформы.
    "parser_tools": ["core.platformsetting"],
}
LEVEL_ACTIONS = {"off": (), "on": (), "view": ("view",), "work": ("view", "add", "change"), "full": ("view", "add", "change", "delete")}
DATA_LEVELS = [("off", "Нет"), ("view", "Просмотр"), ("work", "Работа"), ("full", "Полный")]
PAGE_LEVELS = [("off", "Нет"), ("on", "Открыт")]
LEVEL_HINTS = {
    "off": "раздел скрыт",
    "on": "раздел открыт, его кнопки доступны",
    "view": "только смотреть",
    "work": "смотреть, разбирать, править — без удаления",
    "full": "всё, включая удаление",
}
# Разделы, где даже «Открыт» — серьёзное право: подсвечиваем в редакторе.
SENSITIVE = {"scripts", "users", "platform_settings", "announcements", "mourning", "audit", "admin_bot", "channel"}

# Готовые шаблоны ролей.
PRESETS = {
    "moderator": ("Модератор", "Разбирает накрутки, спорные оценки, обращения болельщиков", {
        "overview": "on", "antifraud": "work", "evaluation_sessions": "view", "data_trust": "work", "users": "view",
        "admin_bot": "on"}),
    "editor": ("Редактор контента", "Мнения экспертов, Telegram-канал, соцсети, объявления", {
        "overview": "on", "experts": "full", "channel": "full", "social_content": "on", "announcements": "work", "matches": "view",
        "admin_bot": "on"}),
    "data": ("Данные и парсер", "Матчи, ФИО, дубли игроков, здоровье и доверие к данным", {
        "overview": "on", "matches": "work", "data_health": "work", "data_trust": "work", "names_review": "work",
        "duplicate_players": "full", "parser_tools": "on", "admin_bot": "on"}),
    "ads": ("Реклама и партнёры", "Баннеры, партнёры, отчёты", {"overview": "on", "traffic": "on", "ads": "full"}),
    "analyst": ("Аналитик", "Только смотреть цифры", {"overview": "on", "traffic": "on", "matches": "view",
                                                      "evaluation_sessions": "view", "audit": "on"}),
}


def levels_for(section: str) -> list[tuple[str, str]]:
    return DATA_LEVELS if section in SECTION_MODELS else PAGE_LEVELS


def sections_layout():
    """Разделы, сгруппированные как в меню дашборда: [(группа, [(ключ, подпись, подсказка)])]."""
    from .access import SUPERUSER_ONLY_SECTIONS
    from .nav import NAV_GROUPS

    return [(g.label, [(i.key, i.label, i.hint) for i in g.items if i.key not in SUPERUSER_ONLY_SECTIONS]) for g in NAV_GROUPS]


def clean_levels(raw: dict) -> dict:
    """Только известные разделы и допустимые уровни; off не храним."""
    out = {}
    for _g, items in sections_layout():
        for key, _label, _hint in items:
            value = raw.get(key, "off")
            if value in dict(levels_for(key)) and value != "off":
                out[key] = value
    return out


def permissions_for(levels: dict):
    """Права Django, которые даёт набор уровней."""
    wanted = set()
    for section, level in levels.items():
        for model in SECTION_MODELS.get(section, []):
            app, name = model.split(".")
            for action in LEVEL_ACTIONS.get(level, ()):
                wanted.add((app, f"{action}_{name}"))
    if not wanted:
        return Permission.objects.none()
    q = Permission.objects.none()
    for app, codename in wanted:
        q = q | Permission.objects.filter(content_type__app_label=app, codename=codename)
    return q


@transaction.atomic
def save_role(group: Group, levels: dict, description: str = ""):
    from .models import StaffRole

    levels = clean_levels(levels)
    role, _ = StaffRole.objects.update_or_create(group=group, defaults={"levels": levels, "description": description[:200]})
    group.permissions.set(permissions_for(levels))
    return role


def create_from_preset(key: str) -> Group:
    name, description, levels = PRESETS[key]
    base, n = name, 2
    while Group.objects.filter(name=name).exists():
        name, n = f"{base} {n}", n + 1
    group = Group.objects.create(name=name)
    save_role(group, levels, description)
    return group


def infer_levels(group: Group) -> dict:
    """Уровни по уже выданным правам — для групп, созданных до ролей."""
    have = set(group.permissions.values_list("content_type__app_label", "codename"))
    levels = {}
    for section, models in SECTION_MODELS.items():
        best = "off"
        for level in ("view", "work", "full"):
            needed = {(m.split(".")[0], f"{a}_{m.split('.')[1]}") for m in models for a in LEVEL_ACTIONS[level]}
            if needed <= have:
                best = level
        if best != "off":
            levels[section] = best
    return levels


def role_levels(group: Group) -> dict:
    role = getattr(group, "staff_role", None)
    return role.levels if role else {}


def role_sections(user) -> set[str]:
    """Разделы из всех ролей сотрудника."""
    from .models import StaffRole

    sections = set()
    for levels in StaffRole.objects.filter(group__user=user).values_list("levels", flat=True):
        sections |= {k for k, v in (levels or {}).items() if v != "off"}
    return sections


def user_access_summary(user) -> list[dict]:
    """Итоговый доступ сотрудника по разделам: уровень и откуда он (роль / индивидуально)."""
    from .models import StaffAccessGrant, StaffRole

    order = {"off": 0, "on": 1, "view": 1, "work": 2, "full": 3}
    best: dict[str, tuple[str, list[str]]] = {}
    for role in StaffRole.objects.filter(group__user=user).select_related("group"):
        for section, level in (role.levels or {}).items():
            cur, src = best.get(section, ("off", []))
            if order.get(level, 0) > order.get(cur, 0):
                cur = level
            best[section] = (cur, src + [role.group.name])
    grant = StaffAccessGrant.objects.filter(user=user).first()
    for section in (grant.allowed_sections if grant else []):
        cur, src = best.get(section, ("off", []))
        if cur == "off":
            # Индивидуально открытый раздел — только просмотр: менять данные можно лишь по роли.
            cur = "on" if section not in SECTION_MODELS else "view"
        best[section] = (cur, src + ["индивидуально"])
    rows = []
    for group_label, items in sections_layout():
        for key, label, _hint in items:
            if key in best:
                level, src = best[key]
                rows.append({"group": group_label, "key": key, "label": label, "level": level,
                             "level_label": dict(DATA_LEVELS + PAGE_LEVELS)[level],
                             "hint": LEVEL_HINTS[level],
                             "sources": src, "sensitive": key in SENSITIVE})
    return rows
