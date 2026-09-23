# dashboard/templatetags/dashboard_extras.py
"""
Шаблонные фильтры для раздела "Скрипты и команды" — в первую очередь
format_command_output (2026-09-22, прямая просьба пользователя: "визуально
не нравится... надо более красиво, понятно и читабельно" — сырой
plain-text <pre> с выводом читать было тяжело).

БАГ, КОТОРЫЙ ТУТ БЫЛ (2026-09-22, живой репорт пользователя — скопировал
вывод diagnose_duplicate_players из браузера и вставил в чат, все
key=value пары оказались склеены БЕЗ пробелов: "id=...sportmonks_id=...").
Причина — по спецификации CSS Flexbox, текстовые узлы, состоящие ТОЛЬКО
из пробельных символов, между прямыми детьми flex-контейнера при рендере
полностью удаляются из дерева — визуально пробел между соседними
<span>-бейджами держался только на CSS `gap`, а не на реальном пробельном
символе, поэтому при выделении/копировании текста браузером пробела там
физически не было. Фикс — разделители между элементами внутри
`display:flex`-контейнеров теперь либо настоящий видимый символ (" · "),
либо контейнер вообще не flex (обычный инлайновый поток, где пробелы
сохраняются браузером как обычно).

Разбирает несколько строго известных форматов (заголовки чанков
verify_names_with_ai, строки кандидатов, строки-отчёты
diagnose_duplicate_players/merge_duplicate_players) в раскрашенный HTML.

2026-09-23, прямая просьба пользователя: "для каждого вида вывода на
странице сделай максимально читабельным и красиво размеченным, где надо —
подсветить/подчеркнуть/выделить". Специфичных regex'ов на ~20 команд не
напасёшься — вместо этого используем разметку, которая УЖЕ есть в каждой
команде: self.style.SUCCESS/WARNING/ERROR/NOTICE/MIGRATE_HEADING
(dashboard/command_runner.py теперь зовёт call_command(..., force_color=
True) — см. докстринг там же про то, почему это раньше терялось). ANSI
SGR-коды (`\x1b[NNm`), которые из-за этого оказываются в run.stdout/
run.stderr, разбираются функцией _strip_ansi ДО прогона через все regex'ы
ниже — сами regex'ы матчат чистый текст, как и раньше, без изменений. Если
строка была стилизована, но ни один специфичный формат её не узнал (это и
есть "длинный хвост" из простых информационных строк большинства команд) —
используем сохранённый ANSI-класс как есть, просто как ЦВЕТ текста
(_render_line, самый конец). Для ЛЮБОГО совсем не стилизованного вывода —
как раньше, моноширинным текстом без раскраски.
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
    """{{ user|can_access_section:"matches" }} — используется в _nav.html,
    чтобы не показывать вкладки разделов, к которым у текущего staff нет
    доступа (dashboard/access.py::user_can_access_section — та же функция,
    которую реально enforce'ит DashboardSectionAccessMiddleware; здесь она
    только про то, что видно в меню, не про саму границу безопасности)."""
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
# 2026-09-22, прямая просьба пользователя (diagnose_duplicate_players
# нечитаем) — специальный разбор для "  id=<uuid> sportmonks_id=... "
# строк: id — отдельная кнопка "скопировать" (клик → clipboard), не голый
# текст (36-значный UUID глазами не читают и руками не копируют).
_PLAYER_RECORD_RE = re.compile(
    r"^  id=([0-9a-fA-F-]{36}) sportmonks_id=(\S+) номер=(\S+) активен=(\S+) источник_фио=(\S+) создан=(\S+)$"
)
# "    тип=протоколы составов=8 событий=1 последний_матч=..." /
# "    тип=вовлечённость оценок=264 агрегатов=8" — см. diagnose_duplicate_players.py.
_TYPED_STAT_RE = re.compile(r"^    тип=(\S+) (.+)$")
_KV_LINE_RE = re.compile(r"^\s*(?:\S+=\S+\s*)+$")
_KV_TOKEN_RE = re.compile(r"(\S+?)=(\S+)")

# ANSI SGR-коды, которые реально встречаются в выводе Django management-
# команд — только то, что генерирует django.utils.termcolors.PALETTES
# (DEFAULT_PALETTE=dark): fg-цвет (30-37), опционально ";1" (bold). Код
# строится в django/utils/termcolors.py::colorize как "<fg>[;1]" — тот же
# порядок здесь. "0" — RESET, сбрасывает текущий стиль.
_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")
_ANSI_CLASS = {
    # SUCCESS (fg=green, bold)
    "32;1": "text-success font-semibold",
    "1;32": "text-success font-semibold",
    # WARNING (fg=yellow, bold)
    "33;1": "text-warning font-semibold",
    "1;33": "text-warning font-semibold",
    # ERROR (fg=red, bold)
    "31;1": "text-error font-semibold",
    "1;31": "text-error font-semibold",
    # NOTICE (fg=red, без bold — используется реже ERROR, тот же цвет, но менее "кричащий")
    "31": "text-error",
    # MIGRATE_HEADING (fg=cyan, bold) — у нас нет миграций в этих командах,
    # но diagnose_nominations.py использует его как раздел-заголовок
    # ("1. Реальные номинации...", "2. Проверка...") — своя обработка ниже
    # (_render_line), не просто цвет текста.
    "36;1": "__heading__",
    "1;36": "__heading__",
    "36": "text-info",
    # голый bold без цвета (MIGRATE_LABEL/SQL_TABLE/HTTP_INFO — в наших
    # командах практически не встречается, но на всякий случай)
    "1": "font-semibold",
    "35;1": "text-secondary font-semibold",
    "1;35": "text-secondary font-semibold",
    "35": "text-secondary",
}


def _strip_ansi(line: str) -> tuple[str, str | None]:
    """Убирает ANSI SGR-коды из строки, возвращает (чистый_текст, css_класс).
    css_класс — None, если строка не была стилизована ни в какой цвет
    (RESET-только код "0" не считается стилем). Django оборачивает СТИЛЕМ
    ВСЮ строку целиком в self.style.X(text) — заходов с несколькими
    цветами внутри одной строки в реальных командах проекта не бывает,
    поэтому достаточно взять первый нетривиальный код."""
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

# Разделитель между элементами ВНУТРИ flex-контейнеров — видимый символ,
# не голый пробел (см. докстринг модуля про баг с flexbox).
_DOT_SEP = mark_safe(' <span class="opacity-20">·</span> ')


@register.filter
def format_command_output(text: str):
    """{{ run.stdout|format_command_output }} — используется в
    _scripts_runs_table.html вместо голого {{ run.stdout }} внутри <pre>."""
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
        # MIGRATE_HEADING — используется как раздел-заголовок вручную
        # написанных диагностических отчётов (diagnose_nominations.py:
        # "1. Реальные номинации...", "2. Проверка..."), тот же визуальный
        # приём, что уже есть у _GENERIC_HEADER_RE ("=== X ===") ниже —
        # разделитель, а не просто цветной текст.
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
        # 2026-09-23, прямая просьба пользователя ("видно только начало
        # UUID, остальное спрятано под ...; но и не огромная кнопка") —
        # ДВЕ проблемы разом: (1) "btn-2xs" не существующий в daisyUI класс
        # (реальные размеры — xs/sm/md/lg), поэтому кнопка рендерилась
        # ДЕФОЛТНЫМ (крупным) размером — вот и "огромная" при одной
        # короткой видимой части id; (2) текст обрезался вручную до 8
        # символов + "…". Фикс: настоящий btn-xs (минимальная реальная
        # высота в daisyUI) + мелкий моноширинный шрифт — весь 36-значный
        # UUID помещается на одну строку компактной кнопки, ничего не
        # скрыто, копирование по клику — как раньше.
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

    # Строка не подошла ни под один известный паттерн — но у большинства
    # команд УЖЕ есть смысловая пометка через self.style.SUCCESS/WARNING/
    # ERROR (см. докстринг модуля) — ansi_class несёт этот цвет, даже когда
    # текст произвольный. Иконка — тот же визуальный язык, что и у
    # "Готово:" (alert-success) выше, но в одну строку, не отдельным
    # блоком: этих строк в выводе команд обычно много подряд (по одной на
    # матч/сущность), полноразмерный alert на каждую был бы слишком тяжёлым.
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

    # Обычная нестилизованная строка — моноширинным текстом, как раньше
    # (обычный инлайновый поток, не flex — пробелы внутри сохраняются
    # браузером сами по себе).
    return format_html('<div class="text-xs font-mono py-0.5 whitespace-pre-wrap">{}</div>', line)
