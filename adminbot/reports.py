# adminbot/reports.py
"""Отчёты бота: утренняя сводка с трендами, недельный отчёт по команде, PDF для партнёра."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone


def _day_bounds(day: date):
    start = timezone.make_aware(datetime.combine(day, datetime.min.time()))
    return start, start + timedelta(days=1)


def _range(day_from: date, days: int):
    start, _ = _day_bounds(day_from)
    return start, start + timedelta(days=days)


def metrics(start, end) -> dict:
    from analytics.models import AnalyticsEvent
    from evaluations.models import EvaluationSession
    from predictions.models import MatchPrediction
    from users.models import User

    events = AnalyticsEvent.objects.filter(created_at__gte=start, created_at__lt=end)
    return {
        "registrations": User.objects.filter(date_joined__gte=start, date_joined__lt=end).count(),
        "active": events.exclude(user__isnull=True).values("user").distinct().count(),
        "visitors": events.exclude(session_id="").values("session_id").distinct().count(),
        "evaluations": EvaluationSession.objects.filter(status="completed", completed_at__gte=start, completed_at__lt=end).count(),
        "predictions": MatchPrediction.objects.filter(created_at__gte=start, created_at__lt=end).count(),
    }


def trend(now: int, before: int) -> str:
    if not before:
        return "" if not now else " · новое"
    diff = (now - before) / before
    arrow = "▲" if diff > 0.02 else ("▼" if diff < -0.02 else "≈")
    return f" · {arrow} {diff:+.0%}"


def retention_d7(day: date) -> tuple[int, int]:
    """(вернулись на 7-й день, зарегистрировались за неделю до day)."""
    from analytics.models import AnalyticsEvent
    from users.models import User

    reg_start, reg_end = _day_bounds(day - timedelta(days=7))
    cohort = list(User.objects.filter(date_joined__gte=reg_start, date_joined__lt=reg_end).values_list("id", flat=True))
    if not cohort:
        return 0, 0
    start, end = _day_bounds(day)
    back = AnalyticsEvent.objects.filter(user_id__in=cohort, created_at__gte=start, created_at__lt=end).values("user").distinct().count()
    return back, len(cohort)


def traffic_sources(start, end, limit: int = 3) -> list[tuple[str, int]]:
    from analytics.models import AnalyticsEvent

    counts: dict[str, set] = {}
    host = urlparse(settings.SITE_URL).hostname or ""
    rows = (AnalyticsEvent.objects.filter(created_at__gte=start, created_at__lt=end).exclude(session_id="")
            .values_list("session_id", "utm_source", "referrer")[:20000])
    for session, utm, ref in rows:
        source = utm or (urlparse(ref).hostname or "").removeprefix("www.") or "прямые"
        if host and source.endswith(host):
            continue
        counts.setdefault(source, set()).add(session)
    return sorted(((k, len(v)) for k, v in counts.items()), key=lambda x: -x[1])[:limit]


def digest_text(day: date | None = None) -> str:
    from .handlers import counts, esc, header

    day = day or timezone.localdate() - timedelta(days=1)
    d = metrics(*_day_bounds(day))
    w = metrics(*_day_bounds(day - timedelta(days=7)))
    week = metrics(*_range(day - timedelta(days=6), 7))
    prev = metrics(*_range(day - timedelta(days=13), 7))
    labels = [("registrations", "👤 Регистрации"), ("active", "🙋 Активные пользователи"), ("visitors", "👀 Визиты"),
              ("evaluations", "⭐ Оценки матчей"), ("predictions", "🔮 Прогнозы")]
    lines = [header() + f"☀️ <b>Сводка за {day:%d.%m}</b> <i>(к тому же дню неделю назад)</i>", ""]
    lines += [f"{label}: <b>{d[k]}</b>{trend(d[k], w[k])}" for k, label in labels]
    lines += ["", "<b>За 7 дней</b> <i>(к предыдущим 7)</i>"]
    lines += [f"{label}: <b>{week[k]}</b>{trend(week[k], prev[k])}" for k, label in labels if k != "visitors"]
    back, cohort = retention_d7(day)
    if cohort:
        lines.append(f"🔁 Удержание 7-го дня: <b>{back / cohort:.0%}</b> ({back} из {cohort})")
    sources = traffic_sources(*_day_bounds(day))
    if sources:
        lines.append("🧭 Откуда пришли: " + ", ".join(f"{esc(s)} {n}" for s, n in sources))
    c = counts()
    queue = [f"{icon} {c[k]}" for k, icon in (("takes", "🎙"), ("flags", "🛡"), ("names", "✍️"), ("dups", "👥"), ("contacts", "✉️")) if c[k]]
    if queue:
        lines += ["", "Ждут разбора: " + "  ".join(queue)]
    return "\n".join(lines)


# ---------------- Недельный отчёт по команде
RISKY = {"deploy", "management_command_triggered", "access_grant_updated", "mourning_changed", "user_banned",
         "platform_setting_changed", "platform_setting_deleted", "sportmonks_sync_toggled", "duplicate_players_merged",
         "evaluation_session_deleted", "system_announcement_sent"}
INACTIVE_DAYS = 30


def inactive_staff():
    from users.models import User

    from .models import BotLink

    cutoff = timezone.now() - timedelta(days=INACTIVE_DAYS)
    seen = dict(BotLink.objects.values_list("user_id", "last_seen"))
    out = []
    for u in User.objects.filter(is_staff=True, is_superuser=False, is_active=True):
        last = max([t for t in (u.last_login, seen.get(u.pk)) if t], default=None)
        if not last or last < cutoff:
            out.append((u, last))
    return out


def weekly_staff_text(now=None) -> tuple[str, list]:
    from dashboard.models import AuditAction, StaffActionLog

    from .handlers import cb, esc, header

    now = now or timezone.now()
    logs = StaffActionLog.objects.filter(created_at__gte=now - timedelta(days=7))
    per_user = logs.values("actor_username").annotate(n=Count("id")).order_by("-n")
    labels = dict(AuditAction.choices)
    lines = [header() + "🗂 <b>Команда за неделю</b>", ""]
    if per_user:
        lines += [f"• {esc(r['actor_username'] or '—')}: {r['n']} действий" for r in per_user[:12]]
    else:
        lines.append("Действий не было.")
    risky = list(logs.filter(Q(action__in=RISKY) | Q(details__mode="apply")).order_by("-created_at")[:10])
    if risky:
        lines += ["", "<b>Рискованные действия</b>"]
        for r in risky:
            lines.append(f"• {timezone.localtime(r.created_at):%d.%m %H:%M} {esc(r.actor_username)}: "
                         f"{esc(labels.get(r.action, r.action))} — {esc(r.target[:60])}")
    rows = []
    idle = inactive_staff()
    if idle:
        lines += ["", f"<b>Не заходили {INACTIVE_DAYS}+ дней</b> — может, закрыть доступ?"]
        for u, last in idle[:8]:
            lines.append(f"• {esc(u.username)} — " + (f"был {timezone.localtime(last):%d.%m.%Y}" if last else "ни разу"))
            rows.append([(f"🚪 Снять доступ: {u.username}"[:60], cb("staff_off", str(u.pk)))])
    return "\n".join(lines), rows


# ---------------- PDF для партнёра (Pillow, без внешних библиотек)
def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(str(settings.BASE_DIR / "static" / "fonts" / name), size)
    except OSError:
        return ImageFont.load_default()


def partner_stats(partner, days: int) -> dict:
    from partners.models import BannerDailyStat

    since = timezone.localdate() - timedelta(days=days - 1)
    stats = BannerDailyStat.objects.filter(banner__partner=partner, date__gte=since)
    per_day = {r["date"]: r for r in stats.values("date").annotate(i=Sum("impressions"), c=Sum("clicks"))}
    per_banner = list(stats.values("banner__title", "banner__zone").annotate(i=Sum("impressions"), c=Sum("clicks")).order_by("-i"))
    series = [(since + timedelta(days=n), per_day.get(since + timedelta(days=n), {}).get("i", 0) or 0,
               per_day.get(since + timedelta(days=n), {}).get("c", 0) or 0) for n in range(days)]
    total_i = sum(s[1] for s in series)
    total_c = sum(s[2] for s in series)
    return {"since": since, "series": series, "banners": per_banner, "impressions": total_i, "clicks": total_c,
            "ctr": total_c / total_i if total_i else 0}


def partner_report_pdf(partner, days: int = 30) -> bytes:
    from PIL import Image, ImageDraw

    data = partner_stats(partner, days)
    W, H, pad = 1240, 1754, 90
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    ink, muted, accent = (17, 24, 39), (107, 114, 128), (22, 163, 74)
    d.text((pad, pad), "DOPX · отчёт для партнёра", font=_font(30), fill=muted)
    d.text((pad, pad + 50), partner.name, font=_font(64, True), fill=ink)
    d.text((pad, pad + 140), f"{data['since']:%d.%m.%Y} — {timezone.localdate():%d.%m.%Y} ({days} дн.)", font=_font(30), fill=muted)
    y = pad + 230
    for i, (label, value) in enumerate((("Показы", f"{data['impressions']:,}".replace(",", " ")),
                                        ("Клики", f"{data['clicks']:,}".replace(",", " ")),
                                        ("CTR", f"{data['ctr']:.2%}"))):
        x = pad + i * 360
        d.rounded_rectangle((x, y, x + 330, y + 170), 24, fill=(243, 244, 246))
        d.text((x + 30, y + 25), label, font=_font(30), fill=muted)
        d.text((x + 30, y + 75), value, font=_font(56, True), fill=ink)
    y += 240
    d.text((pad, y), "Показы по дням", font=_font(34, True), fill=ink)
    y += 60
    chart_h, chart_w = 360, W - 2 * pad
    peak = max([s[1] for s in data["series"]] + [1])
    bar = chart_w / max(len(data["series"]), 1)
    for n, (day, imp, _clk) in enumerate(data["series"]):
        h = int(chart_h * imp / peak)
        x0 = pad + n * bar
        d.rectangle((x0 + 2, y + chart_h - h, x0 + bar - 2, y + chart_h), fill=accent)
        if len(data["series"]) <= 14 or n % 5 == 0:
            d.text((x0, y + chart_h + 10), f"{day:%d.%m}", font=_font(18), fill=muted)
    y += chart_h + 80
    d.text((pad, y), "Баннеры", font=_font(34, True), fill=ink)
    y += 60
    for col, x in (("Баннер", pad), ("Показы", pad + 640), ("Клики", pad + 820), ("CTR", pad + 960)):
        d.text((x, y), col, font=_font(26, True), fill=muted)
    y += 46
    for b in data["banners"][:14]:
        ctr = (b["c"] or 0) / b["i"] if b["i"] else 0
        d.text((pad, y), (b["banner__title"] or "—")[:38], font=_font(26), fill=ink)
        d.text((pad + 640, y), str(b["i"] or 0), font=_font(26), fill=ink)
        d.text((pad + 820, y), str(b["c"] or 0), font=_font(26), fill=ink)
        d.text((pad + 960, y), f"{ctr:.2%}", font=_font(26), fill=ink)
        y += 42
    if not data["banners"]:
        d.text((pad, y), "За период показов не было.", font=_font(26), fill=muted)
    d.text((pad, H - pad), "Показ — баннер был виден на экране не меньше секунды. dopx.kz", font=_font(22), fill=muted)
    out = io.BytesIO()
    img.save(out, "PDF", resolution=150.0)
    return out.getvalue()
