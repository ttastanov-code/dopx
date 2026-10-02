# adminbot/screens.py
"""Экраны и действия бота: матчдень, канал, эксперты, траур, партнёры, дежурство, темы уведомлений.
Регистрируются в handlers.VIEWS / WRITE; права — те же, что в дашборде."""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import telegram as tg
from .handlers import ENV, _pairs, _perm, audit, back, can, cb, esc, header
from .models import TOPICS, BotLink, ChannelConfig, ChannelPost

logger = logging.getLogger(__name__)

DASH = settings.SITE_URL.rstrip("/") + "/staff/dashboard"
MATCH_PERM = "matches.change_match"


def _match(pk):
    from matches.models import Match

    return Match.objects.select_related("home_team", "away_team").filter(pk=pk).first()


def _label(m) -> str:
    return f"{m.home_team.name} – {m.away_team.name}"


# ---------------- Матчдень
STATUS_ICON = {"scheduled": "🕒", "live": "🔴", "finished": "🏁", "postponed": "⏸", "cancelled": "✖️"}


def matchday_view(user, arg=""):
    from matches.models import Match

    now = timezone.now()
    qs = Match.objects.select_related("home_team", "away_team")
    today = list(qs.filter(start_time__date=timezone.localdate()).order_by("start_time"))
    matches = today or list(qs.filter(start_time__gt=now, status="scheduled").order_by("start_time")[:6])
    title = "Сегодня" if today else "Ближайшие матчи"
    lines = [header() + f"⚽ <b>Матчдень · {title}</b>", ""]
    rows = []
    for m in matches[:8]:
        from .channel import kickoff

        score = f" {m.home_score}:{m.away_score}" if m.status in ("live", "finished") else ""
        lines.append(f"{STATUS_ICON.get(m.status, '•')} {kickoff(m)} {esc(_label(m))}{score}")
        rows.append([(f"{m.home_team.name} – {m.away_team.name}", cb("m", str(m.pk)))])
    if not matches:
        lines.append("Матчей в ближайшее время нет.")
    else:
        lines.append("\nНажмите на матч — голосование, ресинк, перенос.")
    out = rows
    if can(user, "overview"):
        out.append([("📋 Брифинг", cb("brief"))])
    return "\n".join(lines), out + back()


def brief_view(user, arg=""):
    from .matchday import briefing_text

    text = briefing_text(timezone.localdate())
    return (text or header() + "Сегодня матчей нет."), back(("← Матчдень", cb("md")))


def match_view(user, pk):
    from evaluations.models import EvaluationSession
    from users.models import SuspiciousActivityFlag

    m = _match(pk)
    if not m:
        return header() + "Матч не найден.", back()
    votes = EvaluationSession.objects.filter(match=m, status="completed").count()
    flags = SuspiciousActivityFlag.objects.filter(match=m, status="pending").count()
    until = timezone.localtime(m.voting_open_until)
    voting = "открыто" if m.voting_open_until > timezone.now() and m.status == "finished" else "закрыто"
    lines = [header() + f"{STATUS_ICON.get(m.status, '')} <b>{esc(_label(m))}</b>",
             f"{m.get_status_display()} · {m.get_score_display()}" + (f" · тур {m.tour}" if m.tour else ""),
             f"Начало: {timezone.localtime(m.start_time):%d.%m %H:%M}",
             f"Голосование {voting} до {until:%d.%m %H:%M} · оценок {votes}",
             "Составы: " + ("есть" if m.has_lineup else "нет")]
    if flags:
        lines.append(f"🛡 Подозрений на накрутку: {flags}")
    rows = []
    if can(user, "matches", MATCH_PERM):
        rows.append([("⏱ Голосование +1 ч", cb("vote_more", pk)), ("🧊 Заморозить", cb("vote_freeze", pk))])
        rows.append([("🔄 Пересинхронизировать", cb("resync", pk)), ("📅 Перенос", cb("postpone", pk))])
    rows.append([("Открыть в дашборде", f"{DASH}/matches/{pk}/")])
    return "\n".join(lines), rows + back(("← Матчдень", cb("md")))


def _save_match(user, m, fields: list, what: str):
    from dashboard.models import AuditAction

    m.save(update_fields=fields + ["updated_at"])
    audit(user, f"{what}: {_label(m)}", {"match_id": str(m.pk)}, action=AuditAction.MATCH_MANUAL_EDIT)


def act_vote_more(user, pk):
    m = _match(pk)
    if not m:
        return "Матч не найден."
    m.voting_open_until = max(m.voting_open_until, timezone.now()) + timedelta(hours=1)
    _save_match(user, m, ["voting_open_until"], "Голосование продлено")
    return f"⏱ Голосование продлено до {timezone.localtime(m.voting_open_until):%d.%m %H:%M}."


def act_vote_freeze(user, pk):
    m = _match(pk)
    if not m:
        return "Матч не найден."
    m.voting_open_until = timezone.now()
    _save_match(user, m, ["voting_open_until"], "Голосование заморожено")
    return "🧊 Голосование закрыто. Открыть снова — «Голосование +1 ч»."


def act_postpone(user, pk):
    m = _match(pk)
    if not m:
        return "Матч не найден."
    m.status = "postponed"
    m.manual_override = True
    _save_match(user, m, ["status", "manual_override"], "Отмечен перенос")
    return "📅 Матч отмечен перенесённым; синхронизация больше не перезапишет статус."


def act_resync(user, pk):
    from dashboard.models import AuditAction
    from dashboard.parser_tools import resync_match

    m = _match(pk)
    if not m:
        return "Матч не найден."
    ok, message = resync_match(m)
    audit(user, f"Ресинк: {_label(m)}", {"match_id": str(m.pk), "ok": ok}, action=AuditAction.MATCH_RESYNC)
    return ("🔄 " if ok else "⚠️ ") + esc(message)


# ---------------- Ошибки и воркеры
def errors_view(user, arg=""):
    from .alerts import recent_errors

    lines = recent_errors()
    body = "\n".join(esc(l[:220]) for l in lines) or "Ошибок в errors.log нет."
    return header() + "📜 <b>Последние ошибки</b>\n\n<pre>" + body + "</pre>", back(("↻", cb("errors")), ("🩺 Сервер", cb("status")))


def act_celery_restart(user, arg=""):
    from dopx.celery import app

    replies = app.control.shutdown(reply=True, timeout=3) or []
    audit(user, "Перезапуск воркеров Celery", {"replied": len(replies)})
    return f"🔄 Воркерам отправлена остановка ({len(replies)} ответили). Docker поднимет их заново за ~30 с."


# ---------------- Канал
def chan_view(user, arg=""):
    from . import channel

    counts = {s: ChannelPost.objects.filter(status=s).count() for s in ("draft", "scheduled", "failed")}
    last = ChannelPost.objects.filter(status="published").order_by("-published_at").first()
    lines = [header() + "📣 <b>Telegram-канал</b>", ""]
    if not channel.configured():
        lines.append("⚠️ Канал пока не подключён — посты сохраняются, но не публикуются."
                     + (" (ADMIN_BOT_CHANNEL_ID в .env)" if user.is_superuser else ""))
    lines += [f"📝 Черновики: {counts['draft']} · 🗓 Запланировано: {counts['scheduled']}"
              + (f" · ⚠️ Ошибки: {counts['failed']}" if counts["failed"] else "")]
    if last:
        lines.append(f"Последний пост: {timezone.localtime(last.published_at):%d.%m %H:%M} — {esc(last.text[:60])}")
    rows = [[(f"📝 Черновики · {counts['draft']}", cb("chan_list", "draft")), (f"🗓 План · {counts['scheduled']}", cb("chan_list", "scheduled"))]]
    if counts["failed"]:
        rows.append([(f"⚠️ С ошибкой · {counts['failed']}", cb("chan_list", "failed"))])
    rows.append([("📤 Опубликованные", cb("chan_list", "published"))])
    tools = []
    if can(user, "channel", "adminbot.add_channelpost"):
        tools += [("✍️ Новый пост", cb("post_new")), ("🏆 Итоги тура", cb("chan_round")), ("🧪 Пробные посты", cb("chan_samples"))]
    tools.append(("⚙️ Автопостинг", cb("chan_modes")))
    rows += _pairs(tools)
    return "\n".join(lines), rows + back()


def chan_list_view(user, status):
    order = ("-published_at",) if status == "published" else ("scheduled_at", "-created_at")
    posts = list(ChannelPost.objects.filter(status=status).order_by(*order)[:10])
    title = dict(ChannelPost.STATUS_CHOICES).get(status, status)
    lines = [header() + f"📣 <b>{esc(title)}</b>", ""]
    rows = []
    for p in posts:
        when = f"{timezone.localtime(p.scheduled_at):%d.%m %H:%M} · " if p.scheduled_at else ""
        plain = re.sub(r"<[^>]+>", "", p.text).replace("\n", " ")
        lines.append(f"<b>#{p.pk}</b> · {when}{esc(p.get_kind_display())}\n{esc(plain[:90])}")
        rows.append([(f"#{p.pk} · {p.get_kind_display()}", cb("post", str(p.pk)))])
    if not posts:
        lines.append("Пусто.")
    return "\n".join(lines), rows + back(("← Канал", cb("chan")))


def post_view(user, pk):
    from . import channel

    p = ChannelPost.objects.filter(pk=pk).first()
    if not p:
        return header() + "Пост не найден.", back(("← Канал", cb("chan")))
    meta = f"{p.get_kind_display()} · {p.get_status_display()}"
    if p.scheduled_at and p.status == "scheduled":
        meta += f" на {timezone.localtime(p.scheduled_at):%d.%m %H:%M}"
    if p.error:
        meta += f"\n⚠️ {esc(p.error)}"
    text = header() + f"📣 <b>Пост</b> · {meta}" + ("\n📷 с картинкой" if p.image or p.tg_file_id else "") + "\n\n" + p.text
    rows = channel.draft_rows(p, cb) if p.status in ("draft", "scheduled", "failed") and can(user, "channel", "adminbot.change_channelpost") else []
    if p.status == "published" and can(user, "channel", "adminbot.delete_channelpost"):
        rows.append([("🗑 Удалить из канала", cb("post_unpub", str(p.pk)))])
    return text, rows + back(("← Канал", cb("chan")))


MODE_ICON = {"auto": "🟢", "approve": "🟡", "off": "⚪️"}
MODE_HINT = {"auto": "бот публикует сам", "approve": "черновик сначала приходит вам", "off": "такие посты не готовятся"}


def chan_modes_view(user, arg=""):
    from .channel import KIND_HINTS, MODES

    cfg = ChannelConfig.get()
    labels = dict(ChannelPost.KIND_CHOICES)
    lines = [header() + "⚙️ <b>Автопостинг в канал</b>", "",
             "🟢 Сразу — бот публикует сам", "🟡 С одобрением — черновик сначала приходит вам", "⚪️ Не готовить — выключено", "",
             "Нажмите на тип поста, чтобы поменять режим."]
    rows = [[(f"{MODE_ICON[cfg.mode(k)]} {labels[k]} — {dict(MODES)[cfg.mode(k)]}", cb("chan_kind", k))] for k in KIND_HINTS]
    if can(user, "channel", "adminbot.delete_channelpost"):
        rows.append([("🌙 Ночью не публиковать: " + ("да" if cfg.quiet_hours else "нет"), cb("chan_mode", "quiet:toggle"))])
    else:
        lines.append("\n<i>Менять режимы может сотрудник с полным доступом к каналу.</i>")
    return "\n".join(lines), rows + back(("← Канал", cb("chan")))


def chan_kind_view(user, kind):
    from .channel import KIND_HINTS, MODES

    if kind not in KIND_HINTS:
        return chan_modes_view(user)
    current = ChannelConfig.get().mode(kind)
    label = dict(ChannelPost.KIND_CHOICES)[kind]
    text = (header() + f"⚙️ <b>{esc(label)}</b>\n<i>Когда: {esc(KIND_HINTS[kind])}</i>\n\n"
            f"Сейчас: {MODE_ICON[current]} <b>{dict(MODES)[current]}</b> — {MODE_HINT[current]}")
    rows = []
    if can(user, "channel", "adminbot.delete_channelpost"):
        rows = [[(("✓ " if m == current else "") + f"{MODE_ICON[m]} {title}", cb("chan_mode", f"{kind}:{m}"))] for m, title in MODES]
    return text, rows + back(("← Все типы", cb("chan_modes")))


def _post(pk):
    return ChannelPost.objects.filter(pk=pk).first()


def act_post_pub(user, pk):
    from . import channel

    p = _post(pk)
    if not p:
        return "Пост не найден."
    ok, message = channel.publish(p, user)
    if ok:
        audit(user, f"Пост в канал: {p.text[:80]}", {"post_id": p.pk})
    return message


def act_post_morning(user, pk):
    from . import channel

    p = _post(pk)
    if not p or p.status not in ("draft", "scheduled", "failed"):
        return "Пост уже опубликован или удалён."
    p.status, p.scheduled_at = "scheduled", channel.morning()
    p.save(update_fields=["status", "scheduled_at", "updated_at"])
    audit(user, f"Пост запланирован: {p.text[:80]}", {"post_id": p.pk})
    return f"🕙 Опубликую {timezone.localtime(p.scheduled_at):%d.%m в %H:%M}."


def act_post_del(user, pk):
    p = _post(pk)
    if not p or p.status == "published":
        return "Опубликованный пост удаляйте в самом канале."
    p.status = "cancelled"
    p.save(update_fields=["status", "updated_at"])
    audit(user, f"Пост удалён: {p.text[:80]}", {"post_id": p.pk})
    return "🗑 Пост удалён из очереди."


def act_post_unpub(user, pk):
    from . import channel

    p = _post(pk)
    if not p:
        return "Пост не найден."
    ok, message = channel.unpublish(p)
    if ok:
        audit(user, f"Пост удалён из канала: {p.text[:80]}", {"post_id": p.pk})
    return message


def act_post_edit(user, pk, text, photo=None):
    p = _post(pk)
    if not p or p.status not in ("draft", "scheduled", "failed"):
        return "Пост уже опубликован или удалён."
    p.text = esc(text)[:4000]
    if photo:
        p.tg_file_id, p.image = photo, ""
    p.save(update_fields=["text", "tg_file_id", "image", "updated_at"])
    return "✏️ Текст обновлён. Проверьте и опубликуйте: /menu → Канал → Черновики."


def act_post_new(user, arg, text, photo=None):
    p = ChannelPost.objects.create(kind="manual", text=esc(text)[:4000], tg_file_id=photo or "", created_by=user)
    audit(user, f"Новый пост в канал (черновик): {text[:80]}", {"post_id": p.pk})
    return f"📝 Черновик готов (#{p.pk}). Откройте его, чтобы опубликовать: Канал → Черновики."


def act_chan_round(user, arg=""):
    from . import channel

    posts = channel.round_posts()
    return f"🏆 Подготовлено постов: {len(posts)}." if posts else "Новых постов по итогам тура нет (или канал не настроен)."


def act_chan_samples(user, arg=""):
    from . import channel

    posts = channel.sample_posts()
    audit(user, f"Пробные посты канала: {len(posts)}", {})
    if not posts:
        return "Не из чего собрать: нет матчей, оценок и мнений."
    return (f"🧪 Готово {len(posts)} пробных черновиков — по одному каждого типа, из настоящих данных. "
            "Откройте «Черновики», посмотрите тексты; любой можно опубликовать, чтобы увидеть, как он выглядит в канале.")


def act_chan_mode(user, arg):
    from .channel import KIND_HINTS, MODES

    kind, _, mode = arg.partition(":")
    cfg = ChannelConfig.get()
    if kind == "quiet":
        cfg.quiet_hours = not cfg.quiet_hours
    elif kind in KIND_HINTS and mode in dict(MODES):
        cfg.modes = {**cfg.modes, kind: mode}
    else:
        return "Неизвестный режим."
    cfg.save()
    audit(user, f"Автопостинг: {kind} → {mode or cfg.quiet_hours}", {"modes": cfg.modes, "quiet": cfg.quiet_hours})
    if kind == "quiet":
        return "🌙 Ночью не публиковать: " + ("да" if cfg.quiet_hours else "нет") + "."
    return f"✅ {dict(ChannelPost.KIND_CHOICES)[kind]}: {MODE_ICON[mode]} {dict(MODES)[mode]}."


# ---------------- Эксперты
def experts_view(user, arg=""):
    from engagement.models import ExpertTake

    from .experts import coverage, coverage_text

    pending = ExpertTake.objects.filter(is_published=False, invite__isnull=False).count()
    data = coverage()
    text = coverage_text(data) if data else header() + "🎙 <b>Эксперты</b>\n\nБлижайших матчей нет."
    text += f"\n\nЖдут проверки: <b>{pending}</b>"
    rows = [[(f"🎙 На проверке · {pending}", cb("takes"))]]
    if can(user, "experts", "engagement.add_expertinvite"):
        rows.append([("➕ Пригласить эксперта", cb("inv_new"))])
        if data and data["missing"]:
            rows.append([("🔗 Ссылки тем, кто не написал", cb("inv_missing"))])
    return text, rows + back()


def inv_new_view(user, arg=""):
    from engagement.models import Expert

    experts = list(Expert.objects.filter(is_active=True).order_by("-updated_at")[:10])
    rows = _pairs([(e.name, cb("inv_exp", str(e.pk))) for e in experts])
    rows.append([("🆕 Новый эксперт (заполнит сам)", cb("inv_exp", "new"))])
    return header() + "➕ <b>Кого приглашаем?</b>", rows + back(("← Эксперты", cb("exp")))


def inv_exp_view(user, arg):
    from engagement.expert_invites import match_groups

    cache.set(f"adminbot:inv:{ENV}:{user.pk}", arg, 15 * 60)
    groups = match_groups()
    rows = [[("Любой матч тура", cb("inv_go", "any"))]]
    lines = [header() + "➕ <b>О каком матче?</b>", ""]
    for label, matches, _with_tour in groups[:2]:
        lines.append(f"<b>{esc(label)}</b>")
        rows += [[(f"{m.home_team.name} – {m.away_team.name}", cb("inv_go", str(m.pk)))] for m in matches[:8]]
    return "\n".join(lines), rows + back(("← Назад", cb("inv_new")))


def act_inv_go(user, arg):
    from engagement.models import Expert

    from .experts import create_invite, forward_text

    chosen = cache.get(f"adminbot:inv:{ENV}:{user.pk}")
    if not chosen:
        return "Выбор эксперта устарел — начните заново: Эксперты → Пригласить."
    expert = None if chosen == "new" else Expert.objects.filter(pk=chosen).first()
    match = None if arg == "any" else _match(arg)
    invite = create_invite(user, expert, match)
    return ("🔗 Ссылка готова — перешлите эксперту сообщение ниже.\n\n"
            f"<code>{esc(forward_text(invite))}</code>")


def act_inv_missing(user, arg=""):
    from .experts import coverage, create_invite, forward_text

    data = coverage()
    if not data or not data["missing"]:
        return "Все обычные эксперты уже написали."
    parts = []
    for expert in data["missing"][:8]:
        invite = create_invite(user, expert, None, days=5, max_takes=len(data["matches"]))
        parts.append(f"<b>{esc(expert.name)}</b>\n<code>{esc(forward_text(invite))}</code>")
    return "🔗 Ссылки на тур — перешлите каждому:\n\n" + "\n\n".join(parts)


def act_take_return(user, pk, text, photo=None):
    from engagement.models import ExpertTake

    from .experts import forward_text

    t = ExpertTake.objects.select_related("invite__expert", "invite__match__home_team", "invite__match__away_team").filter(pk=pk).first()
    if not t:
        return "Мнение не найдено."
    t.is_published = False
    t.review_note = text[:500]
    t.save(update_fields=["is_published", "review_note", "updated_at"])
    if t.invite and t.invite.expires_at < timezone.now() + timedelta(days=1):
        t.invite.expires_at = timezone.now() + timedelta(days=3)
        t.invite.save(update_fields=["expires_at", "updated_at"])
    audit(user, f"Мнение возвращено на правку: {t}", {"take_id": str(t.pk)})
    link = f"\n\n<code>Комментарий редакции DOPX: {esc(text[:500])}\nПоправьте, пожалуйста, по той же ссылке: {esc(forward_text(t.invite).rsplit(chr(10), 1)[-1])}</code>" if t.invite else ""
    return "↩️ Мнение возвращено, эксперт увидит комментарий на странице мнения. Сообщение для эксперта:" + link


# ---------------- Обращения
def act_contact_reply(user, pk, text, photo=None):
    from notifications.models import ContactSubmission, Notification
    from notifications.tasks import send_push_task

    c = ContactSubmission.objects.filter(pk=pk).first()
    if not c:
        return "Обращение не найдено."
    c.admin_response = text[:4000]
    c.status = "resolved"
    c.save(update_fields=["admin_response", "status", "updated_at"])
    title = f"Ответ на обращение «{c.subject[:80]}»"
    if c.user_id:
        Notification.objects.create(user_id=c.user_id, notification_type="contact_reply", title=title[:255], message=text[:2000])
        send_push_task.delay([str(c.user_id)], title[:120], text[:180], "/notifications/", "default", f"contact-{c.id}")
    email = getattr(c, "contact_email", "") or c.guest_email
    if email:
        from django.core.mail import send_mail

        try:
            send_mail(f"{title} | DOPX", f"{text}\n\n— Команда DOPX", None, [email], fail_silently=False)
        except Exception:
            logger.warning("adminbot: письмо с ответом не ушло", exc_info=True)
            email = ""
    audit(user, f"Ответ на обращение: {c.subject[:80]}", {"contact_id": str(c.pk)})
    where = " и ".join(x for x in ("в уведомления на сайте" if c.user_id else "", "на почту" if email else "") if x) or "некуда (нет аккаунта и почты)"
    return f"💬 Ответ отправлен {where}, обращение закрыто."


# ---------------- Расхождения Sportmonks
def act_disc(user, pk, revert: bool):
    from dashboard.models import AuditAction
    from parsers.models import ParserDiscrepancy

    d = ParserDiscrepancy.objects.select_related("match").filter(pk=pk, reviewed=False).first()
    if not d:
        return "Расхождение уже разобрано."
    if revert and d.match:
        old = d.old_value
        value = int(old) if d.field_name in ("home_score", "away_score") and old.lstrip("-").isdigit() else old
        setattr(d.match, d.field_name, value)
        d.match.manual_override = True
        d.match.save(update_fields=[d.field_name, "manual_override", "updated_at"])
    d.reviewed, d.reviewed_by, d.reviewed_at = True, user, timezone.now()
    d.note = "оставили наши данные (бот)" if revert else "приняли данные поставщика (бот)"
    d.save(update_fields=["reviewed", "reviewed_by", "reviewed_at", "note"])
    audit(user, d.match_label, {"discrepancy_id": str(d.pk), "revert": revert}, action=AuditAction.PARSER_DISCREPANCY_REVIEWED)
    return "↩️ Вернули наши данные и закрепили их от синка." if revert else "✅ Данные поставщика приняты."


# ---------------- Траур
def mourning_view(user, arg=""):
    from core.models import MourningMode
    from core.mourning import current_mourning

    obj = MourningMode.objects.filter(pk=1).first()
    active = bool(current_mourning())
    lines = [header() + "🕯 <b>Режим траура</b>", "", "Сейчас: " + ("<b>включён</b>" if active else "выключен")]
    if obj:
        lines.append(f"Плашка: «{esc(obj.message)}»")
    rows = []
    if can(user, "mourning", "core.change_mourningmode"):
        rows.append([("🕯 Включить сейчас", cb("mourn_on"))] if not active else [("Выключить", cb("mourn_off"))])
    rows.append([("Настроить в дашборде", f"{DASH}/mourning/")])
    return "\n".join(lines), rows + back()


def _mourning(user, enable: bool):
    from core.live import bump_data_version
    from core.models import MourningMode
    from core.mourning import forget
    from dashboard.models import AuditAction

    obj, _ = MourningMode.objects.get_or_create(pk=1)
    obj.is_enabled = enable
    if enable:
        obj.starts_at = obj.ends_at = None
    obj.updated_by = user
    obj.save()
    forget()
    bump_data_version()
    audit(user, obj.message, {"is_enabled": enable}, action=AuditAction.MOURNING_CHANGED)
    return "🕯 Режим траура включён: сайт монохромный, реклама и развлекательные push приглушены." if enable else "Режим траура выключен."


# ---------------- Партнёры
def partners_view(user, arg=""):
    from partners.models import Partner

    partners = list(Partner.objects.filter(is_active=True).order_by("name")[:12])
    rows = _pairs([(p.name, cb("partner", str(p.pk))) for p in partners])
    text = header() + "💼 <b>Отчёт для партнёра</b>\n\nВыберите партнёра — пришлю PDF с показами и кликами, его можно сразу переслать."
    return text if partners else header() + "Активных партнёров нет.", rows + back()


def partner_view(user, pk):
    from partners.models import Partner

    p = Partner.objects.filter(pk=pk).first()
    if not p:
        return header() + "Партнёр не найден.", back()
    rows = [[("📄 7 дней", cb("partner_pdf", f"{pk}:7")), ("📄 30 дней", cb("partner_pdf", f"{pk}:30"))]]
    return header() + f"💼 <b>{esc(p.name)}</b>\n\nЗа какой период отчёт?", rows + back(("← Партнёры", cb("partners")))


def partner_pdf_view(user, arg, tid=None):
    from partners.models import Partner

    from .reports import partner_report_pdf, partner_stats

    pk, _, days = arg.partition(":")
    p = Partner.objects.filter(pk=pk).first()
    if not p or days not in ("7", "30"):
        return header() + "Партнёр не найден.", back()
    s = partner_stats(p, int(days))
    caption = f"{esc(p.name)}: {s['impressions']} показов, {s['clicks']} кликов, CTR {s['ctr']:.2%} за {days} дн."
    if tid:
        tg.send_document(tid, partner_report_pdf(p, int(days)), f"DOPX-{p.slug or 'partner'}-{days}d.pdf", caption)
    audit(user, f"PDF-отчёт партнёру: {p.name}", {"days": days})
    return header() + "📄 Отчёт отправлен выше.", back(("← Партнёры", cb("partners")))


# ---------------- Дежурство и темы
def duty_view(user, arg=""):
    from .notify import links_for

    links = sorted(links_for("system_status"), key=lambda l: (l.duty_order or 99, l.user.username))
    lines = [header() + "🧑‍🚒 <b>Дежурство</b>", "",
             "Инциденты сначала получает дежурный №1. Нет «Беру» за 10 минут — следующий, потом все."]
    on = [l for l in links if l.duty_order]
    lines += [""] + ([f"{l.duty_order}. {esc(l.user.username)}" for l in on] or ["Дежурных нет — инциденты получают все."])
    me = BotLink.objects.filter(user=user).first()
    rows = []
    if me and me.duty_order != 1:
        rows.append([("🙋 Я дежурю", cb("duty_me"))])
    if me and me.duty_order:
        rows.append([("Снять с себя дежурство", cb("duty_off"))])
    if user.is_superuser and on:
        rows.append([("Сбросить всех", cb("duty_clear"))])
    return "\n".join(lines), rows + back()


def _duty(user, action: str):
    me = BotLink.objects.filter(user=user).first()
    if action == "duty_clear" and user.is_superuser:
        BotLink.objects.update(duty_order=0)
    elif me and action == "duty_me":
        for l in BotLink.objects.filter(duty_order__gt=0).exclude(pk=me.pk).order_by("duty_order"):
            l.duty_order += 1
            l.save(update_fields=["duty_order"])
        me.duty_order = 1
        me.save(update_fields=["duty_order"])
    elif me and action == "duty_off":
        me.duty_order = 0
        me.save(update_fields=["duty_order"])
        for n, l in enumerate(BotLink.objects.filter(duty_order__gt=0).order_by("duty_order"), 1):
            BotLink.objects.filter(pk=l.pk).update(duty_order=n)
    audit(user, f"Дежурство: {action}", {})
    return duty_view(user)


TOPIC_SHORT = {"matchday": "Матчдень", "live": "Live", "incidents": "Инциденты", "queues": "Очереди",
               "channel": "Канал", "digest": "Сводка", "weekly": "Отчёт недели"}


def topics_view(user, arg=""):
    from .models import DEFAULT_TOPICS

    link = BotLink.objects.filter(user=user).first()
    current = DEFAULT_TOPICS if link is None or link.topics is None else link.topics
    lines = [header() + "🔔 <b>Уведомления</b>", "", "Сейчас: " + ("<b>включены</b>" if link and link.notify else "<b>выключены</b>"), ""]
    lines += [("✅ " if k in current else "▫️ ") + f"<b>{TOPIC_SHORT[k]}</b> — {esc(label)}" for k, label in TOPICS.items()]
    lines += ["", "Нажмите на тему, чтобы включить или выключить. Приходит только то, к чему у вас есть доступ."]
    rows = _pairs([(("✅ " if k in current else "▫️ ") + TOPIC_SHORT[k], cb("topic", k)) for k in TOPICS])
    rows.append([("🔕 Выключить все" if link and link.notify else "🔔 Включить все", cb("notify"))])
    return "\n".join(lines), rows + back()


def _toggle_topic(user, key):
    from .models import DEFAULT_TOPICS

    link = BotLink.objects.filter(user=user).first()
    if link and key in TOPICS:
        current = list(DEFAULT_TOPICS if link.topics is None else link.topics)
        link.topics = [t for t in current if t != key] if key in current else current + [key]
        link.save(update_fields=["topics"])
    return topics_view(user)


# ---------------- Пользователи
def act_staff_off(user, pk):
    from dashboard.models import AuditAction
    from users.models import User

    u = User.objects.filter(pk=pk, is_staff=True, is_superuser=False).first()
    if not u:
        return "Сотрудник не найден."
    u.is_staff = False
    u.save(update_fields=["is_staff"])
    BotLink.objects.filter(user=u).delete()
    audit(user, f"Снят статус сотрудника: {u.username}", {"user_id": str(u.pk)}, action=AuditAction.ACCESS_GRANT_UPDATED)
    return f"🚪 {esc(u.username)} больше не сотрудник: дашборд, /admin и бот закрыты."


# ---------------- Регистрация
def _super(user, arg=None):
    from .handlers import _is_superuser

    return _is_superuser(user)


CHANNEL = "channel"
VIEWS = {
    "md": (_perm("matches", None), matchday_view),
    "brief": (_perm("overview", None), brief_view),
    "m": (_perm("matches", None), match_view),
    "errors": (_perm("system_status", None), errors_view),
    "chan": (_perm(CHANNEL, None), chan_view),
    "chan_list": (_perm(CHANNEL, None), chan_list_view),
    "post": (_perm(CHANNEL, None), post_view),
    "chan_modes": (_perm(CHANNEL, None), chan_modes_view),
    "chan_kind": (_perm(CHANNEL, None), chan_kind_view),
    "exp": (_perm("experts", None), experts_view),
    "inv_new": (_perm("experts", "engagement.add_expertinvite"), inv_new_view),
    "inv_exp": (_perm("experts", "engagement.add_expertinvite"), inv_exp_view),
    "mourn": (_perm("mourning", None), mourning_view),
    "partners": (_perm("ads", None), partners_view),
    "partner": (_perm("ads", None), partner_view),
    "duty": (_perm("system_status", None), duty_view),
    "duty_me": (_perm("system_status", None), lambda u, a: _duty(u, "duty_me")),
    "duty_off": (_perm("system_status", None), lambda u, a: _duty(u, "duty_off")),
    "duty_clear": (_super, lambda u, a: _duty(u, "duty_clear")),
    "topics": (lambda u, a=None: True, topics_view),
    "topic": (lambda u, a=None: True, _toggle_topic),
}
# Экраны, которым нужен чат (шлют файл).
VIEWS_WITH_CHAT = {"partner_pdf": (_perm("ads", None), partner_pdf_view)}
WRITE = {
    "vote_more": (_perm("matches", MATCH_PERM), act_vote_more, None),
    "vote_freeze": (_perm("matches", MATCH_PERM), act_vote_freeze, None),
    "postpone": (_perm("matches", MATCH_PERM), act_postpone, None),
    "resync": (_perm("matches", MATCH_PERM), act_resync, None),
    "celery_restart": (_super, act_celery_restart, None),
    "post_pub": (_perm(CHANNEL, "adminbot.change_channelpost"), act_post_pub, "chan"),
    "post_morning": (_perm(CHANNEL, "adminbot.change_channelpost"), act_post_morning, "chan"),
    "post_del": (_perm(CHANNEL, "adminbot.delete_channelpost"), act_post_del, "chan"),
    "post_unpub": (_perm(CHANNEL, "adminbot.delete_channelpost"), act_post_unpub, "chan"),
    "post_edit": (_perm(CHANNEL, "adminbot.change_channelpost"), act_post_edit, None),
    "post_new": (_perm(CHANNEL, "adminbot.add_channelpost"), act_post_new, None),
    "chan_round": (_perm(CHANNEL, "adminbot.add_channelpost"), act_chan_round, "chan"),
    "chan_samples": (_perm(CHANNEL, "adminbot.add_channelpost"), act_chan_samples, "chan"),
    "chan_mode": (_perm(CHANNEL, "adminbot.delete_channelpost"), act_chan_mode, "chan_modes"),
    "inv_go": (_perm("experts", "engagement.add_expertinvite"), act_inv_go, None),
    "inv_missing": (_perm("experts", "engagement.add_expertinvite"), act_inv_missing, None),
    "take_return": (_perm("experts", "engagement.change_experttake"), act_take_return, "takes"),
    "contact_reply": (_perm("data_trust", "notifications.change_contactsubmission"), act_contact_reply, "contacts"),
    "disc_ok": (_perm("data_trust", "parsers.change_parserdiscrepancy"), lambda u, a: act_disc(u, a, False), None),
    "disc_back": (_perm("data_trust", "parsers.change_parserdiscrepancy"), lambda u, a: act_disc(u, a, True), None),
    "mourn_on": (_perm("mourning", "core.change_mourningmode"), lambda u, a: _mourning(u, True), "mourn"),
    "mourn_off": (_perm("mourning", "core.change_mourningmode"), lambda u, a: _mourning(u, False), "mourn"),
    "staff_off": (_super, act_staff_off, None),
}
# Действия, которым нужен текст сотрудника: после подтверждения бот просит написать его.
TEXT_PROMPTS = {
    "post_edit": "✏️ Пришлите новый текст поста (можно с картинкой).",
    "post_new": "✍️ Пришлите текст поста для канала — можно фото с подписью. Это будет черновик, опубликуете после проверки.",
    "take_return": "↩️ Напишите комментарий эксперту: что поправить.",
    "contact_reply": "💬 Напишите ответ болельщику — он придёт ему на сайт и на почту.",
}
