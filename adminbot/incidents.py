# adminbot/incidents.py
"""Инциденты: сначала дежурному, без «Беру» за ESCALATE_AFTER — следующему, в конце — всем."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from . import telegram as tg
from .models import BotLink, Incident

logger = logging.getLogger(__name__)

ESCALATE_AFTER = timedelta(minutes=10)
# Повтор того же инцидента не раньше, чем через час.
DEDUP_WINDOW = timedelta(hours=1)


def _chain(section: str, perm: str | None) -> list[list[BotLink]]:
    """Ступени эскалации: дежурные по порядку, последняя — все получатели."""
    from .notify import links_for

    links = links_for(section, perm or None, topic="incidents")
    duty = sorted((l for l in links if l.duty_order), key=lambda l: l.duty_order)
    steps = [[l] for l in duty]
    rest = [l for l in links if l not in duty]
    if rest or not steps:
        steps.append(links)
    return [s for s in steps if s]


def _rows(inc: Incident) -> list:
    from .handlers import cb

    rows = [list(map(tuple, r)) for r in (inc.rows or [])]
    if not inc.acked_by_id and not inc.resolved_at:
        rows.append([("🙋 Беру в работу", cb("inc_ack", str(inc.pk)))])
    return rows


def _deliver(inc: Incident, links: list[BotLink], note: str = "") -> None:
    sent = []
    for link in links:
        if any(m["chat"] == link.telegram_id for m in inc.messages):
            continue
        msg = tg.send(link.telegram_id, inc.text + note, _rows(inc))
        if msg:
            sent.append({"chat": link.telegram_id, "mid": msg.get("message_id")})
    if sent:
        Incident.objects.filter(pk=inc.pk).update(messages=inc.messages + sent)
        inc.messages += sent


def open_incident(key: str, title: str, text: str, rows=None, section: str = "system_status", perm: str = "") -> Incident | None:
    """Новый инцидент или None, если такой уже открыт (или был в последний час)."""
    if not tg.enabled():
        return None
    recent = Incident.objects.filter(key=key, created_at__gte=timezone.now() - DEDUP_WINDOW).first()
    if recent or Incident.objects.filter(key=key, resolved_at__isnull=True).exists():
        return None
    steps = _chain(section, perm)
    if not steps:
        return None
    inc = Incident.objects.create(key=key, title=title[:200], text=text, rows=rows or [], section=section, perm=perm,
                                  next_escalation_at=timezone.now() + ESCALATE_AFTER if len(steps) > 1 else None)
    _deliver(inc, steps[0], "\n\n<i>🧑‍🚒 Вы дежурный.</i>" if len(steps) > 1 else "")
    return inc


def escalate_due() -> int:
    """Передать дальше инциденты без реакции. Ступень захватываем атомарно — дублей нет."""
    done = 0
    now = timezone.now()
    for inc in Incident.objects.filter(acked_by__isnull=True, resolved_at__isnull=True, next_escalation_at__lte=now):
        steps = _chain(inc.section, inc.perm)
        nxt = inc.level + 1
        more = nxt + 1 < len(steps)
        claimed = Incident.objects.filter(pk=inc.pk, level=inc.level).update(
            level=nxt, next_escalation_at=now + ESCALATE_AFTER if more else None)
        if not claimed or nxt >= len(steps):
            continue
        inc.level = nxt
        _deliver(inc, steps[nxt], "\n\n<i>⏫ Дежурный не ответил за 10 минут.</i>")
        done += 1
    return done


def _mark(inc: Incident, line: str) -> None:
    for m in inc.messages:
        if m.get("mid"):
            tg.edit(m["chat"], m["mid"], inc.text + "\n\n" + line, _rows(inc))


def ack(inc_id, user) -> str:
    from .handlers import esc

    if not Incident.objects.filter(pk=inc_id, acked_by__isnull=True).update(acked_by=user, acked_at=timezone.now()):
        inc = Incident.objects.filter(pk=inc_id).select_related("acked_by").first()
        who = inc.acked_by.username if inc and inc.acked_by else "кто-то"
        return f"Уже в работе: {esc(who)}."
    inc = Incident.objects.get(pk=inc_id)
    _mark(inc, f"🙋 <b>В работе: {esc(user.username)}</b> · {timezone.localtime():%H:%M}")
    return "🙋 Инцидент ваш. Остальные это видят."


def acked(key: str) -> bool:
    return Incident.objects.filter(key=key, resolved_at__isnull=True, acked_by__isnull=False).exists()


def resolve(key: str, note: str = "✅ Решено") -> list[int]:
    """Закрыть открытые инциденты; вернуть чаты, где они были (туда — сообщение о восстановлении)."""
    chats = []
    for inc in Incident.objects.filter(key=key, resolved_at__isnull=True):
        inc.resolved_at = timezone.now()
        inc.save(update_fields=["resolved_at"])
        _mark(inc, f"<b>{note}</b> · {timezone.localtime():%H:%M}")
        chats += [m["chat"] for m in inc.messages]
    return list(dict.fromkeys(chats))
