# dashboard/templatetags/dashboard_extras.py
"""Фильтры для «Скриптов и команд»: разметка вывода команд.

Команды запускаются с force_color=True — по ANSI-цвету строки (SUCCESS/WARNING/ERROR)
подбираем стиль. Отдельные известные форматы (verify_names_with_ai,
diagnose/merge_duplicate_players) размечаются специально.
Во flex-контейнерах разделители — видимый символ, иначе пробелы теряются при копировании.
"""
from __future__ import annotations

import re
import uuid as uuid_module

from django import template
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def can_access_section(user, section_key: str) -> bool:
    """{{ user|can_access_section:"matches" }} — скрывает недоступные вкладки в меню."""
    from ..access import user_can_access_section as _check
    return _check(user, section_key)

_CHUNK_RE = re.compile(r"^=== чанк (\d+) \(порция ≤(\d+)\) — (.+) ===$")
_CATEGORY_RE = re.compile(r"^(Игрока|Судьи|Тренера): кандидатов на проверку — (\d+)$")
_CANDIDATE_RE = re.compile(r"^  (\S+) (.+?) -> (.+)$")
_ERROR_RE = re.compile(r"^ОШИБКА: (.+)$")
_RESULT_RE = re.compile(r"^(.+?) \(уверенность: (\w+)(, совпадает с текущим)?\)$")
_DONE_RE = re.compile(r"^Готово: (.+)$")
_LIMIT_HIT_RE = re.compile(r"^Достигнут --limit=.+$")
_DRYRUN_RE = re.compile(r"^\[dry-run\] (.+)$")

_GENERIC_HEADER_RE = re.compile(r"^=== (.+) ===$")
# Строки «  id=<uuid> sportmonks_id=...» — id как кнопка копирования.
_PLAYER_RECORD_RE = re.compile(
    r"^  id=([0-9a-fA-F-]{36}) sportmonks_id=(\S+) номер=(\S+) активен=(\S+) источник_фио=(\S+) создан=(\S+)$"
)
# Строки «тип=...» из diagnose_duplicate_players.
_TYPED_STAT_RE = re.compile(r"^    тип=(\S+) (.+)$")
_KV_LINE_RE = re.compile(r"^\s*(?:\S+=\S+\s*)+$")
_KV_TOKEN_RE = re.compile(r"(\S+?)=(\S+)")

# ANSI SGR-коды Django: цвет 30-37, опционально ;1 (bold), 0 — reset.
_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_CLASS = {
    # SUCCESS (green, bold)
    "32;1": "text-success font-semibold",
    "1;32": "text-success font-semibold",
    # WARNING (yellow, bold)
    "33;1": "text-warning font-semibold",
    "1;33": "text-warning font-semibold",
    # ERROR (red, bold)
    "31;1": "text-error font-semibold",
    "1;31": "text-error font-semibold",
    # NOTICE (red)
    "31": "text-error",
    # MIGRATE_HEADING (cyan, bold) — заголовок раздела.
    "36;1": "__heading__",
    "1;36": "__heading__",
    "36": "text-info",
    # bold без цвета
    "1": "font-semibold",
    "35;1": "text-secondary font-semibold",
    "1;35": "text-secondary font-semibold",
    "35": "text-secondary",
}


def _strip_ansi(line: str) -> tuple[str, str | None]:
    """Убирает ANSI-коды, возвращает (текст, css_класс). Класс None — строка без стиля."""
    codes = _ANSI_RE.findall(line)
    plain = _ANSI_RE.sub("", line)
    css_class = None
    for code in codes:
        if code and code != "0":
            css_class = _ANSI_CLASS.get(code)
            if css_class:
                break
    return plain, css_class

_CONFIDENCE_BADGE = {
    "high": "badge-success",
    "medium": "badge-warning",
    "low": "badge-error badge-outline",
}

_STAT_TYPE_LABELS = {
    "протоколы": "Протоколы (Sportmonks)",
    "вовлечённость": "Вовлечённость (community)",
}

_ACTIVE_LABELS = {"True": ("Активен", "badge-success"), "False": ("Неактивен", "badge-ghost")}

# Видимый разделитель внутри flex.
_DOT_SEP = mark_safe(' <span class="opacity-20">·</span> ')


@register.filter
def format_command_output(text: str):
    """{{ run.stdout|format_command_output }}"""
    if not text:
        return ""
    parts = []
    for raw_line in text.split("\n"):
        plain_line, ansi_class = _strip_ansi(raw_line)
        if not plain_line.strip():
            continue
        parts.append(_render_line(plain_line, ansi_class))
    return mark_safe("".join(parts))


def _render_line(line: str, ansi_class: str | None = None) -> str:
    if ansi_class == "__heading__":
        # MIGRATE_HEADING — как заголовок раздела.
        return format_html('<div class="divider text-xs font-semibold text-info my-2">{}</div>', line)

    m = _CHUNK_RE.match(line)
    if m:
        idx, limit, ts = m.groups()
        return format_html(
            '<div class="divider text-[10px] opacity-50 my-2">Чанк {}{}порция ≤{}{}{}</div>',
            idx, _DOT_SEP, limit, _DOT_SEP, ts,
        )

    m = _CATEGORY_RE.match(line)
    if m:
        label, count = m.groups()
        return format_html(
            '<div class="text-[11px] font-bold uppercase tracking-wide opacity-60 mt-3 mb-1 first:mt-0">'
            '{} <span class="badge badge-ghost badge-xs ml-1 normal-case font-normal">{} кандидатов</span></div>',
            label, count,
        )

    m = _DONE_RE.match(line)
    if m:
        return format_html(
            '<div class="alert alert-success text-xs py-2 px-3 mt-2"><i class="ti ti-check"></i> Готово: {}</div>',
            m.group(1),
        )

    m = _DRYRUN_RE.match(line)
    if m:
        return format_html('<div class="text-xs opacity-70 italic py-0.5">[dry-run] {}</div>', m.group(1))

    if _LIMIT_HIT_RE.match(line):
        return format_html('<div class="text-[11px] opacity-50 italic py-0.5">{}</div>', line)

    m = _CANDIDATE_RE.match(line)
    if m:
        entity, name, rest = m.groups()

        err = _ERROR_RE.match(rest)
        if err:
            return format_html(
                '<div class="flex items-start gap-2 py-1 text-xs border-b border-base-200 last:border-0">'
                '<span class="opacity-50 whitespace-nowrap shrink-0">{} {}</span>'
                '<span class="opacity-30">→</span>'
                '<span class="text-error">Ошибка: {}</span></div>',
                entity, name, err.group(1),
            )

        res = _RESULT_RE.match(rest)
        if res:
            new_value, confidence, matches = res.groups()
            if matches:
                return format_html(
                    '<div class="flex items-start gap-2 py-1 text-xs border-b border-base-200 last:border-0 opacity-60">'
                    '<span class="whitespace-nowrap shrink-0">{} {}</span>'
                    '<span class="opacity-30">→</span>'
                    '<span class="italic">без изменений</span></div>',
                    entity, name,
                )
            badge_class = _CONFIDENCE_BADGE.get(confidence, "badge-ghost")
            return format_html(
                '<div class="flex items-start gap-2 py-1 text-xs border-b border-base-200 last:border-0">'
                '<span class="opacity-50 whitespace-nowrap shrink-0">{} {}</span>'
                '<span class="opacity-30">→</span>'
                '<span class="text-success font-medium">{}</span>'
                '<span class="badge {} badge-xs shrink-0">{}</span></div>',
                entity, name, new_value, badge_class, confidence,
            )

        return format_html('<div class="text-xs font-mono py-0.5">{}</div>', line)

    # --- diagnose_duplicate_players / merge_duplicate_players ---
    m = _GENERIC_HEADER_RE.match(line)
    if m:
        return format_html('<div class="divider text-xs font-semibold opacity-70 my-2">{}</div>', m.group(1))

    m = _PLAYER_RECORD_RE.match(line)
    if m:
        player_id, sm_id, number, active, source, created = m.groups()
        try:
            uuid_module.UUID(player_id)
            valid_uuid = True
        except ValueError:
            valid_uuid = False
        active_label, active_badge = _ACTIVE_LABELS.get(active, (active, "badge-ghost"))
        # btn-xs + моноширинный мелкий шрифт — UUID целиком.
        copy_button = (
            format_html(
                '<button type="button" class="btn btn-outline btn-xs font-mono gap-1 text-[10px] leading-none" '
                'data-copy="{0}" onclick="dopxCopyToClipboard(this)" title="Нажмите, чтобы скопировать ID">'
                '<i class="ti ti-copy text-[11px]"></i>{0}</button>',
                player_id,
            )
            if valid_uuid
            else format_html('<span class="font-mono text-xs">id={}</span>', player_id)
        )
        return format_html(
            '<div class="flex flex-wrap items-center gap-2 py-1.5 mt-2 pt-2 border-t border-base-200 first:mt-0 first:border-0 first:pt-0">'
            '{}<span class="badge badge-outline badge-xs font-mono">sm_id={}</span>'
            '<span class="badge badge-outline badge-xs">№{}</span>'
            '<span class="badge {} badge-xs">{}</span>'
            '<span class="text-[10px] opacity-50">{}{}создан {}</span></div>',
            copy_button, sm_id, number, active_badge, active_label, source, _DOT_SEP, created,
        )

    m = _TYPED_STAT_RE.match(line)
    if m:
        label, rest = m.groups()
        pairs = _KV_TOKEN_RE.findall(rest)
        if pairs:
            label_display = _STAT_TYPE_LABELS.get(label, label)
            items = format_html_join(
                _DOT_SEP,
                "{}: <b>{}</b>",
                ((k, v) for k, v in pairs),
            )
            return format_html(
                '<div class="flex items-center gap-2 py-0.5 text-[11px] opacity-70 ml-2">'
                '<span class="badge badge-ghost badge-xs">{}</span><span>{}</span></div>',
                label_display, items,
            )

    if _KV_LINE_RE.match(line) and "=" in line:
        pairs = _KV_TOKEN_RE.findall(line)
        if pairs:
            indent = line.startswith("    ") or line.startswith("\t")
            badges = format_html_join(
                _DOT_SEP,
                '<span class="badge badge-ghost badge-xs font-mono">{}={}</span>',
                ((k, v) for k, v in pairs),
            )
            return format_html(
                '<div class="flex flex-wrap items-center gap-1 py-0.5{}">{}</div>',
                " ml-4" if indent else "", badges,
            )

    # Неизвестный формат, но есть цвет — строка с иконкой.
    if ansi_class:
        icon = ""
        if "text-success" in ansi_class:
            icon = '<i class="ti ti-check text-[11px] shrink-0"></i> '
        elif "text-warning" in ansi_class:
            icon = '<i class="ti ti-alert-triangle text-[11px] shrink-0"></i> '
        elif "text-error" in ansi_class:
            icon = '<i class="ti ti-x text-[11px] shrink-0"></i> '
        return format_html(
            '<div class="text-xs font-mono py-0.5 whitespace-pre-wrap {}">{}{}</div>',
            ansi_class, mark_safe(icon), line,
        )

    # Обычная строка без стиля.
    return format_html('<div class="text-xs font-mono py-0.5 whitespace-pre-wrap">{}</div>', line)
