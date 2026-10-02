# adminbot/handlers.py
"""Обработка апдейта в своём окружении. callback_data: «<env>|<действие>|<аргумент>», env — d/p.
Доступ как в дашборде (раздел «Telegram-бот» + раздел/право); запись — после 2FA."""
from __future__ import annotations

import html
import logging
import os
import re
import shutil
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from core.utils import is_rate_limited

from . import telegram as tg
from .models import BotLink, BotLinkCode

logger = logging.getLogger(__name__)

ENV = settings.ADMIN_BOT_ENV
ENV_LETTER = {"dev": "d", "prod": "p"}
LETTER_ENV = {v: k for k, v in ENV_LETTER.items()}
ENV_LABEL = {"dev": "🧪 DEV · ноутбук", "prod": "🟢 PROD"}
ENV_SHORT = {"dev": "🧪 Dev", "prod": "🟢 Прод"}
ELEVATION_TTL = 10 * 60          # после кода 2FA действия без повторного кода
PENDING_TTL = 3 * 60
RUN_NOTIFY_TTL = 6 * 3600
CODE_RE = re.compile(r"^\d{6}$")


def cb(action: str, arg: str = "") -> str:
    return f"{ENV_LETTER[ENV]}|{action}|{arg}"


def esc(s) -> str:
    return html.escape(str(s))


def header() -> str:
    return f"<b>{ENV_LABEL[ENV]}</b>\n"


def back(*extra) -> list:
    """Нижний ряд: дополнительные кнопки + «← Меню»."""
    return [list(extra) + [("← Меню", cb("menu"))]]


# ---------------- Права
def bot_access(user) -> bool:
    """Отдельный доступ к боту — раздел «Telegram-бот» (суперпользователю — всегда)."""
    from dashboard.access import user_can_access_section

    return user.is_active and user.is_staff and user_can_access_section(user, "admin_bot")


def can(user, section: str, perm: str | None = None) -> bool:
    from dashboard.access import user_can_access_section

    if not bot_access(user) or not user_can_access_section(user, section):
        return False
    # Смотреть данные открытого раздела можно без права view_ — как в дашборде.
    return user.is_superuser or not perm or ".view_" in perm or user.has_perm(perm)


def has_2fa(user) -> bool:
    from django_otp import devices_for_user

    return any(devices_for_user(user, confirmed=True))


def verify_2fa(user, code: str) -> bool:
    from django_otp import devices_for_user

    return any(d.verify_token(code) for d in devices_for_user(user, confirmed=True))


def elevated(user) -> bool:
    return cache.get(f"adminbot:elev:{ENV}:{user.pk}") is not None


def audit(user, target: str, details: dict, action=None) -> None:
    from dashboard.models import AuditAction, StaffActionLog

    try:
        StaffActionLog.objects.create(actor=user, actor_username=user.username, action=action or AuditAction.BOT_ACTION,
                                      target=str(target)[:300], details={"via": "telegram", "env": ENV, **(details or {})})
    except Exception:
        logger.exception("adminbot: audit failed")


# ---------------- Счётчики очередей
QUEUES = [  # ключ, иконка, подпись, раздел, право
    ("takes", "🎙", "Мнения", "experts", "engagement.view_experttake"),
    ("flags", "🛡", "Антифрод", "antifraud", "users.view_suspiciousactivityflag"),
    ("names", "✍️", "ФИО", "names_review", "parsers.view_nameverificationsuggestion"),
    ("dups", "👥", "Дубли", "duplicate_players", "players.view_potentialduplicateplayer"),
    ("contacts", "✉️", "Обращения", "data_trust", "notifications.view_contactsubmission"),
]


def counts() -> dict:
    from engagement.models import ExpertTake
    from notifications.models import ContactSubmission
    from parsers.models import NameVerificationSuggestion
    from players.models import PotentialDuplicatePlayer
    from users.models import SuspiciousActivityFlag

    return {
        "takes": ExpertTake.objects.filter(is_published=False, invite__isnull=False).count(),
        "flags": SuspiciousActivityFlag.objects.filter(status="pending").count(),
        "names": NameVerificationSuggestion.objects.filter(status__in=["pending_review", "check_failed"]).count(),
        "dups": PotentialDuplicatePlayer.objects.filter(reviewed=False).count(),
        "contacts": ContactSubmission.objects.filter(status="new").count(),
    }


def _pairs(buttons) -> list:
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


# ---------------- Меню
def env_row() -> list:
    return [((ENV_SHORT[e] + " ✓") if e == ENV else ENV_SHORT[e], f"env|{e}") for e in ("prod", "dev")]


def health_line() -> str:
    from . import alerts

    problems = alerts.active()
    if problems:
        return "🔴 " + "; ".join(esc(p["title"]) for p in problems[:3])
    from core.health import HEARTBEAT_STALE_SECONDS, celery_heartbeat_age

    age = celery_heartbeat_age()
    celery_ok = age is not None and age < HEARTBEAT_STALE_SECONDS
    return "✅ Всё работает" if celery_ok else "🟠 Celery не отвечает"


def menu_text(user) -> str:
    from . import ask

    c = counts()
    lines = [header() + f"Привет, <b>{esc(user.username)}</b> · версия {esc(settings.APP_VERSION)}", health_line()]
    queue = [f"{icon} {c[key]}" for key, icon, _label, section, perm in QUEUES if can(user, section, perm)]
    if queue:
        lines.append("Ждут разбора: " + "  ".join(queue))
    if ask.enabled():
        lines.append("\n<i>💬 Можно спросить текстом: «кто лучший игрок 12 тура?»</i>")
    return "\n".join(lines)


# Кнопки разделов меню: (текст, действие, раздел).
MENU_TOOLS = [
    ("⚽ Матчдень", "md", "matches"), ("📣 Канал", "chan", "channel"),
    ("🎙 Эксперты", "exp", "experts"), ("🕯 Траур", "mourn", "mourning"),
    ("💼 Партнёры", "partners", "ads"), ("🧑‍🚒 Дежурство", "duty", "system_status"),
    ("⚙️ Скрипты", "scripts", "scripts"),
]


def menu_rows(user) -> list:
    c = counts()
    rows = [[("📊 Сводка", cb("sum")), ("🩺 Сервер", cb("status"))]]
    queue = [(f"{icon} {label} · {c[key]}", cb(key)) for key, icon, label, section, perm in QUEUES if can(user, section, perm)]
    rows += _pairs(queue)
    tools = [(label, cb(action)) for label, action, section in MENU_TOOLS if can(user, section)]
    if user.is_superuser:
        tools.append(("🚀 Деплой", cb("deploy")))
    rows += _pairs(tools)
    link = BotLink.objects.filter(user=user).first()
    rows.append([("🔔 Уведомления" if link and link.notify else "🔕 Уведомления", cb("topics")),
                 ("🌐 Дашборд", settings.SITE_URL.rstrip("/") + "/staff/dashboard/")])
    rows.append(env_row())
    return rows


# ---------------- Экраны
def summary_text(day=None) -> str:
    from evaluations.models import EvaluationSession
    from matches.models import Match
    from predictions.models import MatchPrediction
    from users.models import User

    today = day or timezone.localdate()
    start = timezone.make_aware(timezone.datetime.combine(today, timezone.datetime.min.time()))
    end = start + timedelta(days=1)
    matches = Match.objects.filter(start_time__gte=start, start_time__lt=end)
    c = counts()
    return "\n".join([
        header() + f"📊 <b>Сводка за {today:%d.%m}</b>",
        "",
        f"👤 Регистраций: <b>{User.objects.filter(date_joined__gte=start, date_joined__lt=end).count()}</b>",
        f"⭐ Оценок матчей: <b>{EvaluationSession.objects.filter(status='completed', completed_at__gte=start, completed_at__lt=end).count()}</b>",
        f"🔮 Прогнозов: <b>{MatchPrediction.objects.filter(created_at__gte=start, created_at__lt=end).count()}</b>",
        f"⚽ Матчей: <b>{matches.count()}</b> · сыграно {matches.filter(status='finished').count()}",
        "",
        "<b>Ждут разбора</b>",
        f"🎙 Мнения экспертов: {c['takes']}",
        f"🛡 Флаги антифрода: {c['flags']}",
        f"✍️ Правки ФИО: {c['names']}",
        f"👥 Дубли игроков: {c['dups']}",
        f"✉️ Обращения: {c['contacts']}",
    ])


def _memory() -> str:
    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        total, avail = info["MemTotal"] / 1048576, info["MemAvailable"] / 1048576
        return f"{total - avail:.1f} из {total:.1f} ГБ"
    except (OSError, KeyError, ValueError):
        return "нет данных"


def status_text() -> str:
    from core.health import HEARTBEAT_STALE_SECONDS, _check_cache, _check_db, celery_heartbeat_age
    from dashboard.infra_services import _redis_stats, deploy_history

    from . import alerts

    ok = lambda v: "✅" if v else "❌"  # noqa: E731
    age = celery_heartbeat_age()
    disk = shutil.disk_usage(settings.BASE_DIR)
    rs = _redis_stats()
    lines = [header() + "🩺 <b>Сервер</b>"]
    problems = alerts.active()
    if problems:
        lines += ["", "<b>Активные алерты</b>"] + [("🔴 " if p.get("critical") else "🟠 ") + esc(p["title"]) for p in problems]
    lines += [
        "",
        f"Версия <b>{esc(settings.APP_VERSION)}</b> · {esc(settings.APP_COMMIT[:7] if settings.APP_COMMIT else '—')}",
        f"{ok(_check_db())} База   {ok(_check_cache())} Кэш   {ok(rs.get('ok'))} Redis",
        f"{ok(age is not None and age < HEARTBEAT_STALE_SECONDS)} Celery · " + (f"пульс {age} с назад" if age is not None else "пульса нет"),
        f"📥 Очередь задач: {rs.get('queue_depth', '—')}",
        f"💾 Диск: свободно {disk.free / 1e9:.0f} из {disk.total / 1e9:.0f} ГБ",
        f"🧠 Память: {_memory()}",
    ]
    try:
        lines.append("📈 Нагрузка: " + " · ".join(f"{x:.2f}" for x in os.getloadavg()))
    except OSError:
        pass
    deploys = deploy_history(1)
    if deploys:
        d = deploys[0]
        lines.append(f"🚀 Деплой: {esc(d.get('status_label', d.get('status')))} · {esc(d.get('version') or d.get('sha', ''))}"
                     + (f" · {d['started']:%d.%m %H:%M}" if d.get("started") else ""))
    folder = settings.BASE_DIR / "backups"
    backups = sorted(folder.glob("*.sql.gz"), key=lambda p: p.stat().st_mtime) if folder.exists() else []
    if backups:
        b = backups[-1]
        when = timezone.datetime.fromtimestamp(b.stat().st_mtime, tz=timezone.get_current_timezone())
        lines.append(f"🗄 Бэкап: {when:%d.%m %H:%M} · {b.stat().st_size / 1e6:.1f} МБ")
    lines.append("" if alerts.enabled() else "\n<i>Алерты в этом окружении выключены.</i>")
    return "\n".join(lines)


def _empty(text):
    return header() + text, back()


def takes_view(user):
    from engagement.models import ExpertTake

    qs = ExpertTake.objects.filter(is_published=False, invite__isnull=False).select_related("match__home_team", "match__away_team", "expert")
    t = qs.order_by("created_at").first()
    if not t:
        return _empty("🎙 Мнений на проверке нет.")
    text = (header() + f"🎙 <b>Мнение на проверке</b> · 1 из {qs.count()}\n\n"
            f"<b>{esc(t.display_name)}</b>{(' · ' + esc(t.display_title)) if t.display_title else ''}\n"
            f"⚽ {esc(t.match.home_team.name)} – {esc(t.match.away_team.name)}\n\n"
            + (f"<b>«{esc(t.headline)}»</b>\n" if t.headline else "") + esc(t.text[:1500]))
    rows = []
    if can(user, "experts", "engagement.change_experttake"):
        rows.append([("✅ Опубликовать", cb("take_pub", str(t.pk))), ("↩️ На правку", cb("take_return", str(t.pk)))])
    if can(user, "experts", "engagement.delete_experttake"):
        rows.append([("🗑 Удалить", cb("take_del", str(t.pk)))])
    rows.append([("Открыть в дашборде", settings.SITE_URL.rstrip("/") + f"/staff/dashboard/experts/takes/{t.pk}/")])
    return text, rows + back()


def flags_view(user):
    from users.models import SuspiciousActivityFlag

    qs = SuspiciousActivityFlag.objects.filter(status="pending").select_related("user", "match")
    f = qs.order_by("-score").first()
    if not f:
        return _empty("🛡 Флагов на проверке нет.")
    target = f.user.username if f.user else str(f.content_object or "—")
    text = (header() + f"🛡 <b>Флаг антифрода</b> · 1 из {qs.count()}\n\n"
            f"{esc(f.get_source_display())}\n\nЦель: <b>{esc(target)}</b>\nПодозрительность: <b>{f.score:.0%}</b>"
            + (f"\nМатч: {esc(f.match)}" if f.match else ""))
    rows = []
    if can(user, "antifraud", "users.change_suspiciousactivityflag"):
        rows.append([("⛔ Накрутка", cb("flag_ok", str(f.pk))), ("👌 Ложный", cb("flag_no", str(f.pk)))])
    return text, rows + back()


def names_view(user):
    from parsers.models import NameVerificationSuggestion

    qs = NameVerificationSuggestion.objects.filter(status__in=["pending_review", "check_failed"])
    s = qs.order_by("created_at").first()
    if not s:
        return _empty("✍️ Правок ФИО на проверке нет.")
    role = s.get_entity_label_display()
    was = f"{s.current_first_name} {s.current_last_name}".strip()
    if s.status == "check_failed":
        text = (header() + f"✍️ <b>Проверка ФИО</b> · 1 из {qs.count()}\n\n{esc(role)}: <b>{esc(was)}</b>\n\n"
                f"⚠️ Gemini не ответил: {esc(s.error_message[:300])}")
        rows = [[("🙈 Скрыть ошибку", cb("name_no", str(s.pk)))]] if can(user, "names_review", "parsers.change_nameverificationsuggestion") else []
        return text, rows + back()
    now = f"{s.suggested_first_name} {s.suggested_last_name}".strip()
    text = (header() + f"✍️ <b>Проверка ФИО</b> · 1 из {qs.count()}\n\n{esc(role)}\n"
            f"Сейчас: <b>{esc(was)}</b>\nПредлагают: <b>{esc(now)}</b>\n"
            f"Уверенность: {esc(s.get_confidence_display() or '—')}"
            + (f"\n\n<i>{esc(s.reasoning[:500])}</i>" if s.reasoning else ""))
    rows = []
    if can(user, "names_review", "parsers.change_nameverificationsuggestion"):
        rows.append([("✅ Принять", cb("name_ok", str(s.pk))), ("✖️ Отклонить", cb("name_no", str(s.pk)))])
    return text, rows + back()


def dups_view(user):
    from players.models import PotentialDuplicatePlayer

    qs = PotentialDuplicatePlayer.objects.filter(reviewed=False).select_related("existing_player__team", "new_player__team")
    f = qs.order_by("created_at").first()
    if not f:
        return _empty("👥 Дублей игроков нет.")

    def card(n, p):
        return (f"{n}. <b>{esc(p.full_name)}</b> · {esc(p.team.name if p.team_id else '—')}\n"
                f"    матчей {p.matchlineupplayer_set.count()} · событий {p.events.count()} · оценок {p.player_evaluations.count()}")
    text = (header() + f"👥 <b>Возможный дубль</b> · 1 из {qs.count()}\n\n"
            + card(1, f.existing_player) + "\n" + card(2, f.new_player)
            + "\n\nОставьте правильную запись — вторая сольётся в неё и удалится.")
    rows = []
    if _can_merge(user):
        rows.append([("Оставить 1", cb("dup_keep", f"{f.pk}:existing")), ("Оставить 2", cb("dup_keep", f"{f.pk}:new"))])
    if can(user, "duplicate_players", "players.change_potentialduplicateplayer"):
        rows.append([("🙅 Разные люди", cb("dup_diff", str(f.pk)))])
    return text, rows + back()


def contacts_view(user):
    from notifications.models import ContactSubmission

    qs = ContactSubmission.objects.filter(status="new").select_related("user")
    c = qs.order_by("created_at").first()
    if not c:
        return _empty("✉️ Новых обращений нет.")
    who = c.user.username if c.user else (c.guest_email or "гость")
    text = (header() + f"✉️ <b>Обращение</b> · 1 из {qs.count()}\n\n"
            f"От: {esc(who)} · {esc(c.get_category_display())}\n<b>{esc(c.subject)}</b>\n\n{esc(c.message[:1500])}")
    rows = []
    if can(user, "data_trust", "notifications.change_contactsubmission"):
        rows.append([("💬 Ответить", cb("contact_reply", str(c.pk))), ("✅ Решено", cb("contact_done", str(c.pk)))])
    if c.related_match_id and can(user, "matches"):
        rows.append([("⚽ Матч", cb("m", str(c.related_match_id)))])
    return text, rows + back()


# ---------------- Скрипты
def bot_scripts() -> dict:
    """Команды, которые можно запускать из бота: без обязательных параметров и без «впиши имя»."""
    from dashboard.commands_registry import CONFIRM_TEXT_COMMANDS, categories

    out = {}
    for key, label, specs in categories():
        ok = [s for s in specs if s.name not in CONFIRM_TEXT_COMMANDS and not any(a.required for a in s.args)]
        if ok:
            out[key] = (label, ok)
    return out


DANGER = {"readonly": "🟢 только чтение", "safe": "🟡 меняет данные безопасно", "destructive": "🔴 опасная"}


def scripts_view(user):
    groups = bot_scripts()
    text = (header() + "⚙️ <b>Скрипты</b>\n\nТе же команды, что в дашборде → Скрипты. Здесь — только без обязательных "
            "параметров; остальные запускайте в дашборде. Результат придёт сообщением.")
    rows = _pairs([(label, cb("scr_cat", key)) for key, (label, _specs) in groups.items()])
    return text, rows + back()


def scripts_cat_view(user, key):
    label, specs = bot_scripts().get(key, ("", []))
    rows = [[(("🔴 " if s.danger == "destructive" else "") + s.label, cb("scr", s.name))] for s in specs]
    return header() + f"⚙️ <b>{esc(label)}</b>", rows + back(("← Скрипты", cb("scripts")))


def script_view(user, name):
    spec = next((s for _l, specs in bot_scripts().values() for s in specs if s.name == name), None)
    if not spec:
        return _empty("Эта команда недоступна в боте.")
    text = (header() + f"⚙️ <b>{esc(spec.label)}</b>\n<code>{esc(spec.name)}</code> · {DANGER.get(spec.danger, spec.danger)}\n\n"
            f"{esc(spec.description)}")
    rows = []
    if _can_run(user, spec):
        if spec.has_apply_flag:
            rows.append([("🔍 Проверка", cb("scr_run", f"{name}:dry")), ("✅ Применить", cb("scr_run", f"{name}:apply"))])
        else:
            rows.append([("▶️ Запустить", cb("scr_run", f"{name}:run"))])
    else:
        text += "\n\n<i>Опасная команда — запуск только суперпользователю.</i>"
    return text, rows + back(("← Назад", cb("scr_cat", spec.category)))


def _can_run(user, spec) -> bool:
    from dashboard.command_runner import can_run

    return can(user, "scripts") and can_run(user, spec)


# ---------------- Деплой
def deploy_view(user):
    from dashboard.infra_services import deploy_history

    from . import github

    lines = [header() + "🚀 <b>Деплой на прод</b>", "",
             "«Выкатить main» — GitHub Actions берёт последний код из ветки main, прогоняет тесты и, если всё зелёное, "
             "обновляет сайт на сервере (5–10 минут). «↩️ vX.Y.Z» — вернуть сайт на прошлую версию.",
             "<i>Пока сервер не подключён (секреты DEPLOY_* в GitHub), шаг деплоя пропускается — пройдут только проверки.</i>",
             "", f"Сейчас здесь: версия <b>{esc(settings.APP_VERSION)}</b>"]
    for d in deploy_history(3):
        lines.append(f"• {esc(d.get('version') or d.get('sha', ''))} — {esc(d.get('status_label', d.get('status')))}"
                     + (f", {d['started']:%d.%m %H:%M}" if d.get("started") else ""))
    if not github.configured():
        lines += ["", "Чтобы выкатывать и откатывать из бота, задайте в .env "
                      "<code>ADMIN_BOT_GITHUB_TOKEN</code> и <code>ADMIN_BOT_GITHUB_REPO</code>."]
        return "\n".join(lines), back()
    rows = [[("🚀 Выкатить main", cb("deploy_ask", "main"))]]
    try:
        runs = github.runs(3)
        if runs:
            lines += ["", "<b>Запуски в GitHub</b>"]
            icons = {"success": "✅", "failure": "❌", "cancelled": "⏹", None: "⏳"}
            for r in runs:
                lines.append(f"{icons.get(r['conclusion'], '•')} {esc(r['title'][:40])} · {esc(r['created'])}")
        current = f"v{settings.APP_VERSION}"
        tags = [r["tag"] for r in github.releases(5) if r["tag"] != current][:4]
        rows += _pairs([(f"↩️ {t}", cb("deploy_ask", t)) for t in tags])
        if tags:
            lines += ["", "Откат: выберите версию."]
    except Exception as e:
        lines += ["", f"⚠️ GitHub не ответил: {esc(str(e)[:120])}"]
    return "\n".join(lines), rows + back()


def deploy_ask_view(user, ref):
    what = "последний <b>main</b> (с тестами)" if ref == "main" else f"версию <b>{esc(ref)}</b> (откат, без ожидания тестов)"
    text = header() + f"🚀 Выкатить на прод {what}?\n\nСайт обновится примерно за 5–10 минут, итог придёт сообщением."
    return text, [[("✅ Да, выкатить", cb("deploy_go", ref)), ("Отмена", cb("deploy"))]]


# ---------------- Действия, меняющие данные (нужен 2FA)
def act_take_pub(user, pk):
    from engagement.models import ExpertTake

    t = ExpertTake.objects.filter(pk=pk).first()
    if not t:
        return "Мнение уже удалено."
    t.is_published = True
    t.review_note = ""
    t.save(update_fields=["is_published", "review_note", "updated_at"])
    audit(user, f"Мнение опубликовано: {t}", {"take_id": str(t.pk)})
    return "✅ Мнение опубликовано."


def act_take_del(user, pk):
    from engagement.models import ExpertTake

    t = ExpertTake.objects.filter(pk=pk).first()
    if not t:
        return "Мнение уже удалено."
    label = str(t)
    t.delete()
    audit(user, f"Мнение удалено: {label}", {"take_id": str(pk)})
    return "🗑 Мнение удалено."


def _flag(user, pk, confirm: bool):
    from aggregates.tasks import apply_divergence_dismissal, schedule_recalculation_for_flags
    from users.models import SuspiciousActivityFlag

    f = SuspiciousActivityFlag.objects.filter(pk=pk, status="pending").first()
    if not f:
        return "Флаг уже разобран."
    f.status = "confirmed" if confirm else "dismissed"
    f.reviewed_by = user
    f.reviewed_at = timezone.now()
    f.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])
    if not confirm:
        apply_divergence_dismissal([f])
    schedule_recalculation_for_flags([f])
    audit(user, f"Флаг {'подтверждён' if confirm else 'отклонён'}", {"flag_id": str(f.pk), "source": f.source})
    return "⛔ Накрутка подтверждена, матчи пересчитаются." if confirm else "👌 Флаг отклонён."


def act_contact_done(user, pk):
    from notifications.models import ContactSubmission

    c = ContactSubmission.objects.filter(pk=pk).first()
    if not c:
        return "Обращение не найдено."
    c.status = "resolved"
    c.save(update_fields=["status", "updated_at"])
    from notifications.tasks import notify_contact_resolved
    notify_contact_resolved(c)
    audit(user, f"Обращение решено: {c.subject}", {"contact_id": str(c.pk)})
    return "✅ Отмечено решённым."


def _review(user, result):
    if result.action:
        audit(user, result.target, result.details, action=result.action)
    return ("✅ " if result.ok else "⚠️ ") + result.message


def act_name(user, pk, approve: bool):
    from dashboard import review_actions
    from parsers.models import NameVerificationSuggestion

    s = NameVerificationSuggestion.objects.filter(pk=pk).first()
    if not s:
        return "Предложение уже разобрано."
    return _review(user, review_actions.approve_name(s, user) if approve else review_actions.reject_name(s, user))


def act_dup_keep(user, arg):
    from dashboard import review_actions
    from players.models import PotentialDuplicatePlayer

    pk, _, side = arg.partition(":")
    f = PotentialDuplicatePlayer.objects.filter(pk=pk).first()
    return _review(user, review_actions.merge_duplicate(f, side, user)) if f else "Флаг уже разобран."


def act_dup_diff(user, pk):
    from dashboard import review_actions
    from players.models import PotentialDuplicatePlayer

    f = PotentialDuplicatePlayer.objects.filter(pk=pk).first()
    return _review(user, review_actions.dismiss_duplicate(f, user)) if f else "Флаг уже разобран."


def act_script(user, arg, tid=None):
    from dashboard.command_runner import ValidationError, build_command_args
    from dashboard.models import AuditAction, ManagementCommandRun
    from dashboard.tasks import run_management_command

    name, _, mode = arg.partition(":")
    spec = next((s for _l, specs in bot_scripts().values() for s in specs if s.name == name), None)
    if not spec or not _can_run(user, spec):
        return "Эта команда недоступна."
    try:
        positional, kwargs = build_command_args(spec, {}, apply=(mode == "apply"))
    except ValidationError as e:
        return "Нужны параметры — запустите в дашборде: " + "; ".join(e.errors)
    run = ManagementCommandRun.objects.create(command_name=spec.name, args={"positional": positional, "kwargs": kwargs},
                                              triggered_by=user, triggered_by_username=user.username)
    run.celery_task_id = run_management_command.delay(str(run.id)).id
    run.save(update_fields=["celery_task_id"])
    if tid:
        watch = cache.get(f"adminbot:watch:{ENV}") or []
        cache.set(f"adminbot:watch:{ENV}", watch + [{"run": str(run.id), "chat": tid}], RUN_NOTIFY_TTL)
    audit(user, spec.name, {"run_id": str(run.id), "mode": mode}, action=AuditAction.MANAGEMENT_COMMAND_TRIGGERED)
    what = {"dry": "проверка (без изменений)", "apply": "с применением", "run": ""}.get(mode, "")
    return f"▶️ «{spec.label}» запущена {what}. Пришлю результат, когда закончится."


def check_runs() -> int:
    """Скрипты, запущенные из бота: закончились — шлём итог. Вызывает процесс бота, а не воркер Celery:
    ответ не зависит от версии кода в воркере."""
    from dashboard.models import ManagementCommandRun

    from .notify import command_finished

    watch = cache.get(f"adminbot:watch:{ENV}") or []
    if not watch:
        return 0
    runs = {str(r.id): r for r in ManagementCommandRun.objects.filter(id__in=[w["run"] for w in watch])}
    left, sent = [], 0
    for w in watch:
        run = runs.get(w["run"])
        if run is None:
            continue
        if run.status in (ManagementCommandRun.Status.SUCCESS, ManagementCommandRun.Status.FAILED):
            command_finished(w["chat"], run)
            sent += 1
        else:
            left.append(w)
    cache.set(f"adminbot:watch:{ENV}", left, RUN_NOTIFY_TTL)
    return sent


DEPLOY_WATCH_TTL = 2 * 3600
DEPLOY_GIVE_UP = 90 * 60
JOB_ICON = {"success": "✅", "failure": "❌", "cancelled": "⏹", "skipped": "⏭"}


def act_deploy(user, ref, tid=None):
    from . import github

    if ref != "main" and not re.match(r"^v\d+\.\d+\.\d+$", ref):
        return "Неверная версия."
    since = timezone.now().replace(microsecond=0) - timedelta(seconds=30)
    github.dispatch("" if ref == "main" else ref)
    audit(user, f"Деплой: {ref}", {"ref": ref})
    if tid:
        key = f"adminbot:deploy_watch:{ENV}"
        watch = cache.get(key) or []
        cache.set(key, watch + [{"chat": tid, "ref": ref, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                 "started": timezone.now().timestamp()}], DEPLOY_WATCH_TTL)
    return (("🚀 Деплой запущен" if ref == "main" else f"↩️ Откат на {ref} запущен")
            + ". Слежу за запуском в GitHub — итог пришлю сюда (обычно 5–10 минут).")


def deploy_result_text(ref, report) -> str:
    title = "Деплой main" if ref == "main" else f"Откат на {ref}"
    ok = report["conclusion"] == "success"
    lines = [header() + ("✅" if ok else "❌") + f" <b>{esc(title)}</b>: " + ("готово" if ok else "не прошёл"), ""]
    for name, state in report["jobs"]:
        lines.append(f"{JOB_ICON.get(state, '•')} {esc(name)}")
    if report["deploy_skipped"]:
        lines += ["", "⏭ Сайт не обновлялся: сервер ещё не подключён (секреты DEPLOY_* в GitHub). Проверки кода пройдены."
                  if ok else "⏭ До сервера не дошло: он ещё не подключён."]
    elif ok:
        lines += ["", "Сайт обновлён."]
    return "\n".join(lines)


def check_deploys() -> int:
    """Запуски деплоя из бота: найти запуск в GitHub, дождаться конца, прислать итог."""
    from . import github

    key = f"adminbot:deploy_watch:{ENV}"
    watch = cache.get(key) or []
    if not watch or not github.configured():
        return 0
    left, sent = [], 0
    for w in watch:
        try:
            run = w.get("run") or (github.find_dispatched(w["since"]) or {}).get("id")
            report = github.run_report(run) if run else None
        except Exception as e:
            logger.warning("adminbot: GitHub недоступен при проверке деплоя: %s", e)
            left.append(w)
            continue
        if report and report["status"] == "completed":
            tg.send(w["chat"], deploy_result_text(w["ref"], report), [[("Открыть в GitHub", report["url"])], [("← Меню", cb("menu"))]])
            sent += 1
        elif timezone.now().timestamp() - w["started"] > DEPLOY_GIVE_UP:
            tg.send(w["chat"], header() + "⏳ Деплой идёт дольше полутора часов — проверьте в GitHub Actions.",
                    [[("Открыть в GitHub", report["url"])]] if report else None)
        else:
            left.append({**w, "run": run} if run else w)
    cache.set(key, left, DEPLOY_WATCH_TTL)
    return sent


def _is_superuser(user, arg=None):
    return user.is_superuser and bot_access(user)


def _perm(section, perm):
    return lambda user, arg=None: can(user, section, perm)


def _can_merge(user, arg=None):
    return can(user, "duplicate_players", "players.change_potentialduplicateplayer") and (
        user.is_superuser or user.has_perm("players.delete_player"))


def _can_script(user, arg=None):
    name = (arg or "").partition(":")[0]
    spec = next((s for _l, specs in bot_scripts().values() for s in specs if s.name == name), None)
    return bool(spec) and _can_run(user, spec)


# действие -> (проверка прав, функция, экран после)
WRITE = {
    "take_pub": (_perm("experts", "engagement.change_experttake"), act_take_pub, "takes"),
    "take_del": (_perm("experts", "engagement.delete_experttake"), act_take_del, "takes"),
    "flag_ok": (_perm("antifraud", "users.change_suspiciousactivityflag"), lambda u, a: _flag(u, a, True), "flags"),
    "flag_no": (_perm("antifraud", "users.change_suspiciousactivityflag"), lambda u, a: _flag(u, a, False), "flags"),
    "contact_done": (_perm("data_trust", "notifications.change_contactsubmission"), act_contact_done, "contacts"),
    "name_ok": (_perm("names_review", "parsers.change_nameverificationsuggestion"), lambda u, a: act_name(u, a, True), "names"),
    "name_no": (_perm("names_review", "parsers.change_nameverificationsuggestion"), lambda u, a: act_name(u, a, False), "names"),
    "dup_keep": (_can_merge, act_dup_keep, "dups"),
    "dup_diff": (_perm("duplicate_players", "players.change_potentialduplicateplayer"), act_dup_diff, "dups"),
    "scr_run": (_can_script, act_script, None),
    "deploy_go": (_is_superuser, act_deploy, None),
}
# экран -> (проверка прав, функция(user[, arg]))
VIEWS = {
    "sum": (_perm("overview", None), lambda u, a: (summary_text(), back(("↻", cb("sum"))))),
    "status": (_perm("system_status", None), lambda u, a: (status_text(), back(("↻ Обновить", cb("status"))))),
    "takes": (_perm("experts", "engagement.view_experttake"), lambda u, a: takes_view(u)),
    "flags": (_perm("antifraud", "users.view_suspiciousactivityflag"), lambda u, a: flags_view(u)),
    "names": (_perm("names_review", "parsers.view_nameverificationsuggestion"), lambda u, a: names_view(u)),
    "dups": (_perm("duplicate_players", "players.view_potentialduplicateplayer"), lambda u, a: dups_view(u)),
    "contacts": (_perm("data_trust", "notifications.view_contactsubmission"), lambda u, a: contacts_view(u)),
    "scripts": (_perm("scripts", None), lambda u, a: scripts_view(u)),
    "scr_cat": (_perm("scripts", None), scripts_cat_view),
    "scr": (_perm("scripts", None), script_view),
    "deploy": (_is_superuser, lambda u, a: deploy_view(u)),
    "deploy_ask": (_is_superuser, deploy_ask_view),
}


# ---------------- Вход
def handle(update: dict) -> None:
    """Апдейт для этого окружения (напрямую от Telegram или через ретранслятор)."""
    msg = update.get("message")
    q = update.get("callback_query")
    chat = (msg or (q or {}).get("message") or {}).get("chat", {})
    if chat.get("type") != "private":
        return  # только личка
    who = (msg or q or {}).get("from", {})
    tid = who.get("id")
    if not tid:
        return
    if is_rate_limited(f"adminbot:{ENV}:{tid}", 40, 60):
        return
    link = BotLink.objects.select_related("user").filter(telegram_id=tid).first()
    if msg:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        photo = msg["photo"][-1]["file_id"] if msg.get("photo") else None
        if text.startswith("/link"):
            return _link(tid, who, text)
        if not _allowed(tid, link):
            return
        _on_message(tid, text, link, photo)
    elif q:
        if not _allowed(tid, link, q):
            return
        _on_callback(tid, q, link)


def _allowed(tid, link, q=None) -> bool:
    if q:
        tg.answer(q["id"])
    if not link:
        tg.send(tid, header() + "Этот бот — для команды DOPX.\n\nЧтобы привязать аккаунт, откройте в дашборде "
                                "<b>Система → Telegram-бот</b>, получите код и пришлите сюда: <code>/link 12345678</code>",
                [env_row()])
        return False
    if not bot_access(link.user):
        tg.send(tid, header() + "🔒 Доступ к боту для вашего аккаунта выключен. Его выдаёт администратор: "
                                "Система → Доступы → раздел «Telegram-бот».")
        return False
    BotLink.objects.filter(pk=link.pk).update(last_seen=timezone.now())
    return True


def _await_key(tid) -> str:
    return f"adminbot:await:{ENV}:{tid}"


def _on_message(tid, text, link, photo=None):
    from . import ask

    user = link.user
    if text in ("/cancel", "отмена", "Отмена"):
        cache.delete(_await_key(tid))
        cache.delete(f"adminbot:pending:{ENV}:{tid}")
        return tg.send(tid, menu_text(user), menu_rows(user))
    waiting = cache.get(_await_key(tid))
    if CODE_RE.match(text) and not waiting:
        return _on_code(tid, user, text)
    if waiting and (text or photo):
        cache.delete(_await_key(tid))
        if not elevated(user):
            return tg.send(tid, header() + "🔐 Подтверждение истекло — начните действие заново.", back())
        return _run_write(tid, user, waiting["action"], waiting["arg"], text=text, photo=photo)
    if text == "/unlink":
        link.delete()
        audit(user, "Отвязан Telegram", {"telegram_id": tid})
        return tg.send(tid, header() + "Аккаунт отвязан от бота.")
    if ask.enabled() and text and not text.startswith("/") and len(text) > 3:
        return _ask(tid, user, text)
    tg.send(tid, menu_text(user), menu_rows(user))


def _ask(tid, user, question):
    """Ответ Claude в отдельном потоке: долгий запрос не держит цикл бота."""
    import threading

    from django.db import close_old_connections

    from . import ask

    if is_rate_limited(f"adminbot:ask:{user.pk}", 20, 3600):
        return tg.send(tid, header() + "Лимит вопросов — 20 в час. Попробуйте позже.")
    tg.send(tid, "🤔 Смотрю данные…")

    def work():
        try:
            reply = ask.answer(user, question)
            tg.send(tid, header() + esc(reply), back())
        except Exception:
            logger.exception("adminbot: ask failed")
            tg.send(tid, header() + "Не получилось ответить, подробности в логах.")
        finally:
            if threading.current_thread() is not threading.main_thread():
                close_old_connections()

    threading.Thread(target=work, daemon=True).start()


def _link(tid, who, text):
    parts = text.split()
    code = parts[1] if len(parts) > 1 else ""
    if is_rate_limited(f"adminbot:link:{tid}", 5, 600):
        return tg.send(tid, header() + "Слишком много попыток. Попробуйте через 10 минут.")
    lc = BotLinkCode.objects.select_related("user").filter(code=code).first()
    if not lc or not lc.is_valid() or not bot_access(lc.user):
        return tg.send(tid, header() + "Код не подошёл или истёк. Получите новый в дашборде.")
    lc.used_at = timezone.now()
    lc.save(update_fields=["used_at"])
    name = " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x) or who.get("username", "")
    BotLink.objects.filter(Q(user=lc.user) | Q(telegram_id=tid)).delete()
    BotLink.objects.create(user=lc.user, telegram_id=tid, telegram_name=name[:120])
    audit(lc.user, "Привязан Telegram", {"telegram_id": tid})
    note = "" if has_2fa(lc.user) else "\n\n⚠️ 2FA не включена — кнопки, меняющие данные, работать не будут."
    tg.send(tid, header() + f"✅ Готово, <b>{esc(lc.user.username)}</b> привязан.{note}")
    tg.send(tid, menu_text(lc.user), menu_rows(lc.user))


def _on_code(tid, user, code):
    pending = cache.get(f"adminbot:pending:{ENV}:{tid}")
    if not pending:
        return tg.send(tid, menu_text(user), menu_rows(user))
    if is_rate_limited(f"adminbot:otp:{user.pk}", 5, 300):
        return tg.send(tid, header() + "Слишком много неверных кодов. Подождите 5 минут.")
    if not verify_2fa(user, code):
        return tg.send(tid, header() + "❌ Код неверный. Пришлите код ещё раз.")
    cache.set(f"adminbot:elev:{ENV}:{user.pk}", 1, ELEVATION_TTL)
    cache.delete(f"adminbot:pending:{ENV}:{tid}")
    if pending["action"] in TEXT_PROMPTS:
        return _ask_text(tid, pending["action"], pending["arg"])
    _run_write(tid, user, pending["action"], pending["arg"])


def _ask_text(tid, action, arg):
    cache.set(_await_key(tid), {"action": action, "arg": arg}, PENDING_TTL * 3)
    tg.send(tid, header() + TEXT_PROMPTS[action] + "\n\n<i>Отмена — /cancel</i>")


def _run_write(tid, user, action, arg, text=None, photo=None):
    check, fn, after = WRITE[action]
    if not check(user, arg):
        return tg.send(tid, header() + "Нет прав на это действие.")
    try:
        if action == "scr_run":
            result = act_script(user, arg, tid)
        elif action == "deploy_go":
            result = act_deploy(user, arg, tid)
        elif action in TEXT_PROMPTS:
            result = fn(user, arg, text or "", photo)
        else:
            result = fn(user, arg)
    except Exception as e:
        logger.exception("adminbot: action %s failed", action)
        result = f"⚠️ Не получилось: {esc(str(e)[:300]) or type(e).__name__}"
    if after:
        text, rows = VIEWS[after][1](user, "")
        return tg.send(tid, header() + result + "\n\n" + text.removeprefix(header()), rows)
    tg.send(tid, header() + result, back())


def _on_callback(tid, q, link):
    parts = q.get("data", "").split("|", 2)
    if len(parts) != 3 or parts[0] != ENV_LETTER[ENV]:
        return
    user, action, arg = link.user, parts[1], parts[2]
    mid = q.get("message", {}).get("message_id")
    if action == "menu":
        return tg.edit(tid, mid, menu_text(user), menu_rows(user))
    if action == "notify":
        link.notify = not link.notify
        link.save(update_fields=["notify"])
        text, rows = VIEWS["topics"][1](user, "")
        return tg.edit(tid, mid, text, rows)
    if action == "inc_ack":
        from . import incidents
        return tg.answer(q["id"], incidents.ack(arg, user), alert=True)
    if action in VIEWS_WITH_CHAT:
        check, view = VIEWS_WITH_CHAT[action]
        if not check(user, arg):
            return tg.send(tid, header() + "Нет доступа к этому разделу.")
        text, rows = view(user, arg, tid)
        return tg.send(tid, text, rows)
    if action == "mute":
        from . import alerts
        if can(user, "system_status"):
            alerts.mute(arg)
            tg.send(tid, header() + "🔕 Этот алерт заглушён на час.")
        return
    if action in VIEWS:
        check, view = VIEWS[action]
        if not check(user, arg):
            return tg.send(tid, header() + "Нет доступа к этому разделу.")
        text, rows = view(user, arg)
        return tg.edit(tid, mid, text, rows)
    if action in WRITE:
        check, _fn, _after = WRITE[action]
        if not check(user, arg):
            return tg.send(tid, header() + "Нет прав на это действие.")
        if not has_2fa(user):
            return tg.send(tid, header() + "🔐 Включите 2FA в дашборде: без неё бот не меняет данные.")
        if not elevated(user):
            cache.set(f"adminbot:pending:{ENV}:{tid}", {"action": action, "arg": arg}, PENDING_TTL)
            return tg.send(tid, header() + "🔐 Подтвердите действие: пришлите код из приложения 2FA (6 цифр).")
        if action in TEXT_PROMPTS:
            return _ask_text(tid, action, arg)
        return _run_write(tid, user, action, arg)


# Экраны и действия из screens.py — в общие реестры.
from .screens import TEXT_PROMPTS, VIEWS_WITH_CHAT  # noqa: E402
from .screens import VIEWS as _MORE_VIEWS  # noqa: E402
from .screens import WRITE as _MORE_WRITE  # noqa: E402

VIEWS.update(_MORE_VIEWS)
WRITE.update(_MORE_WRITE)
