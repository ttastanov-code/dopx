# adminbot/experts.py
"""Эксперты из бота: ссылки-приглашения, покрытие ближайшего тура мнениями, напоминание в среду."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

INVITE_DAYS = 7
USUAL_TOURS = 3     # «обычные» эксперты — писали хотя бы раз за столько последних туров


def invite_link(invite) -> str:
    return settings.SITE_URL.rstrip("/") + reverse("engagement:expert_write", args=[invite.token])


def forward_text(invite) -> str:
    from engagement.expert_invites import share_text

    return f"{share_text(invite)}\n{invite_link(invite)}"


def create_invite(user, expert=None, match=None, days: int = INVITE_DAYS, max_takes: int | None = None):
    from engagement.models import ExpertInvite

    from .handlers import audit

    invite = ExpertInvite.objects.create(
        expert=expert, match=match, expires_at=timezone.now() + timedelta(days=days),
        max_takes=max_takes or (1 if match else 5), note="из Telegram-бота", created_by=user)
    from dashboard.models import AuditAction

    audit(user, f"Ссылка для эксперта: {expert or 'новый'}", {"invite_id": str(invite.pk)}, action=AuditAction.EXPERT_INVITE_CREATED)
    return invite


def upcoming_tour():
    """(тур, матчи) ближайшего тура в течение недели; без номера тура — матчи недели."""
    from matches.models import Match

    now = timezone.now()
    qs = (Match.objects.filter(status="scheduled", start_time__gt=now, start_time__lte=now + timedelta(days=7))
          .select_related("home_team", "away_team").order_by("start_time"))
    first = qs.first()
    if not first:
        return None, []
    if first.tour:
        return first.tour, list(qs.filter(season=first.season, tour=first.tour))
    return None, list(qs)


def coverage() -> dict | None:
    from engagement.models import Expert, ExpertTake

    tour, matches = upcoming_tour()
    if not matches:
        return None
    season = matches[0].season
    takes = ExpertTake.objects.filter(match__in=matches)
    written = set(takes.exclude(expert__isnull=True).values_list("expert_id", flat=True))
    usual = Expert.objects.filter(is_active=True)
    if tour:
        usual = usual.filter(takes__match__season=season, takes__match__tour__gte=tour - USUAL_TOURS,
                             takes__match__tour__lt=tour)
    else:
        usual = usual.filter(takes__created_at__gte=timezone.now() - timedelta(days=21))
    usual = list(usual.distinct())
    return {"tour": tour, "matches": matches, "takes": takes.count(), "usual": usual,
            "missing": [e for e in usual if e.pk not in written]}


def coverage_text(data: dict) -> str:
    from .handlers import esc, header

    what = f"тур {data['tour']}" if data["tour"] else "ближайшие матчи"
    lines = [header() + f"🎙 <b>Мнения на {what}</b>", "",
             f"Написано: <b>{data['takes']}</b> · матчей {len(data['matches'])} · обычно пишут {len(data['usual'])}"]
    if data["missing"]:
        lines.append("Ещё не написали: " + ", ".join(esc(e.name) for e in data["missing"]))
    elif data["usual"]:
        lines.append("Все обычные эксперты уже написали 👍")
    return "\n".join(lines)
