# adminbot/matchday.py
"""Матчдень: события матча (старт, гол, финал, перенос), брифинг перед игровым днём,
отчёт после матча, всплески оценок, анонс тура и итоги тура в канал."""
from __future__ import annotations

import ipaddress
import logging
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.db.models import Avg, Count
from django.utils import timezone

from . import channel
from .models import BotEvent

logger = logging.getLogger(__name__)

BRIEF_BEFORE = timedelta(hours=2)
REPORT_AFTER = timedelta(hours=4)       # от начала матча: финал + время на оценки
PREVIEW_BEFORE = timedelta(hours=24)
NOT_STARTED_AFTER = timedelta(minutes=20)
SURGE_WINDOW = timedelta(minutes=5)
SURGE_MIN = 40                           # оценок за окно
SURGE_RATIO = 5                          # во столько раз выше обычного темпа за час
SURGE_SUBNET_SHARE = 0.3


def _h():
    from .handlers import cb, esc, header

    return cb, esc, header


def _label(m) -> str:
    return f"{m.home_team.name} – {m.away_team.name}"


def match_rows(match, cb) -> list:
    pk = str(match.pk)
    return [
        [("⏱ Голосование +1 ч", cb("vote_more", pk)), ("🔄 Пересинхронизировать", cb("resync", pk))],
        [("Матч в дашборде", settings.SITE_URL.rstrip("/") + f"/staff/dashboard/matches/{pk}/")],
    ]


# ---------------- События матча (из сигнала Match)
def on_match_change(match, before: dict) -> None:
    """before — статус и счёт до сохранения. Каждое событие отправляется один раз."""
    from .notify import push

    cb, esc, header = _h()
    old_status, new_status = before.get("status"), match.status
    score = (match.home_score, match.away_score)
    if old_status == "scheduled" and new_status == "live" and BotEvent.once(f"start:{match.pk}"):
        push(header() + f"▶️ <b>Начался:</b> {esc(_label(match))}", None, "matches", topic="live")
    if new_status == "live" and old_status == "live" and score != (before.get("home_score"), before.get("away_score")):
        if BotEvent.once(f"goal:{match.pk}:{score[0]}:{score[1]}"):
            push(header() + f"⚽ <b>{esc(match.home_team.name)} {score[0]}:{score[1]} {esc(match.away_team.name)}</b>",
                 None, "matches", topic="live")
    if new_status == "finished" and old_status != "finished" and BotEvent.once(f"finish:{match.pk}"):
        until = timezone.localtime(match.voting_open_until)
        push(header() + f"🏁 <b>Финал:</b> {esc(match.home_team.name)} {score[0]}:{score[1]} {esc(match.away_team.name)}\n"
             f"Голосование открыто до {until:%d.%m %H:%M}.", match_rows(match, cb), "matches", topic="matchday")
        channel.result_post(match)
    if new_status in ("postponed", "cancelled") and old_status != new_status and BotEvent.once(f"{new_status}:{match.pk}"):
        what = "перенесён" if new_status == "postponed" else "отменён"
        push(header() + f"⚠️ <b>Матч {what}:</b> {esc(_label(match))}", match_rows(match, cb), "matches", topic="matchday")
        channel.change_post(match, new_status)


# ---------------- Брифинг
def briefing_text(day) -> str | None:
    from engagement.models import ExpertTake
    from matches.models import Match
    from parsers.models import ParserSyncRun
    from partners.models import Banner

    from .handlers import health_line

    _cb, esc, header = _h()
    start = timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time()))
    matches = list(Match.objects.filter(start_time__gte=start, start_time__lt=start + timedelta(days=1))
                   .exclude(status__in=["cancelled", "postponed"]).select_related("home_team", "away_team").order_by("start_time"))
    if not matches:
        return None
    takes = ExpertTake.objects.filter(match__in=matches)
    now = timezone.now()
    banners = Banner.objects.filter(is_active=True).exclude(starts_at__gt=now).exclude(ends_at__lt=now).count()
    last_sync = ParserSyncRun.objects.filter(source="sportmonks").order_by("-started_at").first()
    lines = [header() + f"📋 <b>Брифинг матчдня · {day:%d.%m}</b>", ""]
    for m in matches:
        lineup = "✅ составы" if m.has_lineup else "⏳ составов нет"
        lines.append(f"{channel.kickoff(m, '%H:%M').replace(' (время уточняется)', '')} {esc(_label(m))} · {lineup}")
    published = takes.filter(is_published=True).count()
    pending = takes.filter(is_published=False).count()
    lines += [
        "",
        f"🎙 Мнения экспертов: {published} опубликовано" + (f", {pending} ждут проверки" if pending else "")
        + (" — <b>пока ни одного</b>" if not published and not pending else ""),
        f"📢 Активных баннеров: {banners}",
        "🔄 Sportmonks: " + (f"последний синк {timezone.localtime(last_sync.started_at):%d.%m %H:%M}" if last_sync else "синков не было"),
        health_line(),
    ]
    return "\n".join(lines)


def run_briefing(now=None) -> bool:
    from matches.models import Match

    from .notify import push

    cb, _esc, _header = _h()
    now = now or timezone.now()
    first = (Match.objects.filter(start_time__gt=now, start_time__lte=now + BRIEF_BEFORE, status="scheduled")
             .order_by("start_time").first())
    if not first:
        return False
    day = timezone.localtime(first.start_time).date()
    earlier = Match.objects.filter(start_time__date=day, start_time__lt=first.start_time).exclude(status__in=["cancelled", "postponed"])
    if earlier.exists() or not BotEvent.once(f"brief:{day}"):
        return False
    text = briefing_text(day)
    if text:
        push(text, [[("🎙 Пригласить эксперта", cb("inv_new")), ("🩺 Сервер", cb("status"))]], "overview", topic="matchday")
    return bool(text)


# ---------------- Отчёт после матча
def match_report(match) -> tuple[str, list, list, object, int]:
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display
    from evaluations.models import EvaluationSession
    from users.models import SuspiciousActivityFlag

    cb, esc, header = _h()
    votes = EvaluationSession.objects.filter(match=match, status="completed").count()
    recent = (EvaluationSession.objects.filter(status="completed", match__status="finished",
                                               match__start_time__gte=timezone.now() - timedelta(days=30))
              .exclude(match=match).values("match").annotate(n=Count("id")).aggregate(avg=Avg("n"))["avg"])
    aggs = list(PlayerMatchAggregate.objects.filter(match=match, total_votes__gte=min_votes_for_display())
                .select_related("player").order_by("-performance_score"))
    best, worst = aggs[:3], (aggs[-1] if len(aggs) > 3 else None)
    flags = SuspiciousActivityFlag.objects.filter(match=match, status="pending").count()
    lines = [header() + f"📊 <b>Отчёт: {esc(match.home_team.name)} {match.home_score}:{match.away_score} {esc(match.away_team.name)}</b>",
             "", f"⭐ Оценок: <b>{votes}</b>"]
    if recent:
        diff = (votes - recent) / recent if recent else 0
        lines[-1] += f" · в среднем за месяц {recent:.0f} ({'+' if diff >= 0 else ''}{diff:.0%})"
    if best:
        lines += ["", "<b>Лучшие по мнению болельщиков</b>"]
        lines += [f"{i + 1}. {esc(a.player.full_name)} — {a.performance_score:.1f}" for i, a in enumerate(best)]
    if worst:
        lines.append(f"🥶 Антигерой: {esc(worst.player.full_name)} — {worst.performance_score:.1f}")
    if flags:
        lines += ["", f"🛡 Подозрений на накрутку: <b>{flags}</b>"]
    rows = [[("🛡 Флаги", cb("flags"))]] if flags else []
    return "\n".join(lines), rows, best, worst, votes


def run_reports(now=None) -> int:
    from matches.models import Match

    from .notify import push

    now = now or timezone.now()
    done = 0
    for match in (Match.objects.filter(status="finished", start_time__lte=now - REPORT_AFTER, start_time__gte=now - timedelta(days=1))
                  .select_related("home_team", "away_team")):
        if not BotEvent.once(f"report:{match.pk}"):
            continue
        text, rows, best, worst, votes = match_report(match)
        push(text, rows or None, "overview", topic="matchday")
        if best:
            channel.ratings_post(match, best, worst, votes)
        done += 1
    return done


# ---------------- Проблемы матчдня
def _subnet(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    return str(ipaddress.ip_network(f"{ip}/{24 if addr.version == 4 else 48}", strict=False))


def run_surges(now=None) -> int:
    from evaluations.models import EvaluationSession
    from matches.models import Match

    from .incidents import open_incident

    cb, esc, header = _h()
    now = now or timezone.now()
    found = 0
    for match in Match.objects.filter(status="finished", voting_open_until__gt=now).select_related("home_team", "away_team"):
        done = EvaluationSession.objects.filter(match=match, status="completed")
        window = list(done.filter(completed_at__gte=now - SURGE_WINDOW).values_list("ip_address", flat=True))
        if len(window) < SURGE_MIN:
            continue
        hour = done.filter(completed_at__gte=now - timedelta(hours=1), completed_at__lt=now - SURGE_WINDOW).count()
        usual = hour / 11 if hour else 0      # средний темп за 5 минут в предыдущем часе
        subnets = Counter(s for s in (_subnet(ip) for ip in window if ip) if s)
        top, top_n = (subnets.most_common(1)[0] if subnets else ("", 0))
        share = top_n / len(window)
        # Сразу после финала темп и так растёт с нуля — тогда решает только доля одной подсети.
        if share < SURGE_SUBNET_SHARE and not (usual >= 2 and len(window) >= usual * SURGE_RATIO):
            continue
        bucket = int(now.timestamp() // 1800)
        text = (header() + f"🚨 <b>Всплеск оценок:</b> {esc(_label(match))}\n"
                f"{len(window)} оценок за 5 минут (обычно ~{usual:.0f})"
                + (f"\n{share:.0%} из одной подсети {esc(top)}" if share >= SURGE_SUBNET_SHARE else ""))
        rows = [[("🧊 Заморозить голосование", cb("vote_freeze", str(match.pk))), ("🛡 Флаги", cb("flags"))]]
        if open_incident(f"surge:{match.pk}:{bucket}", f"Всплеск оценок: {_label(match)}", text, rows, "antifraud"):
            found += 1
    return found


def run_not_started(now=None) -> int:
    from matches.models import Match

    from .incidents import open_incident

    cb, esc, header = _h()
    now = now or timezone.now()
    found = 0
    for match in Match.objects.filter(status="scheduled", start_time__lte=now - NOT_STARTED_AFTER,
                                      start_time__gte=now - timedelta(hours=6)).select_related("home_team", "away_team"):
        text = (header() + f"⏰ <b>Матч не начался по данным поставщика</b>\n{esc(_label(match))}, "
                f"начало было в {timezone.localtime(match.start_time):%H:%M}.")
        rows = [[("🔄 Пересинхронизировать", cb("resync", str(match.pk))), ("📅 Отметить перенос", cb("postpone", str(match.pk)))]]
        if open_incident(f"notstarted:{match.pk}", f"Матч не начался: {_label(match)}", text, rows, "matches"):
            found += 1
    return found


# ---------------- Канал: анонс и итоги тура
def run_preview(now=None) -> bool:
    from matches.models import Match

    now = now or timezone.now()
    first = Match.objects.filter(status="scheduled", start_time__gt=now, start_time__lte=now + PREVIEW_BEFORE).order_by("start_time").first()
    if not first:
        return False
    qs = Match.objects.filter(season=first.season, status="scheduled").select_related("home_team", "away_team").order_by("start_time")
    if first.tour:
        if Match.objects.filter(season=first.season, tour=first.tour, start_time__lt=now).exists():
            return False   # тур уже идёт
        matches = list(qs.filter(tour=first.tour))
    else:
        matches = list(qs.filter(start_time__lte=first.start_time + timedelta(days=3)))
    return bool(matches and channel.preview_post(first.tour, matches))


def on_round_final(rnd) -> None:
    from .notify import push

    cb, esc, header = _h()
    if not BotEvent.once(f"roundfinal:{rnd.pk}"):
        return
    lines = [header() + f"🏆 <b>{rnd.tour}-й тур закрыт</b>"]
    if rnd.player_of_round_name:
        lines.append(f"⭐ Игрок тура: {esc(rnd.player_of_round_name)}"
                     + (f" — {rnd.player_of_round_score:.1f}" if rnd.player_of_round_score is not None else ""))
    if rnd.most_dramatic_match_id:
        lines.append(f"🔥 Самый драматичный матч: {esc(_label(rnd.most_dramatic_match))}")
    posts = channel.round_posts(rnd=rnd)
    if posts:
        lines.append(f"\n📣 Для канала готово постов: {len(posts)}")
    push("\n".join(lines), [[("📣 Канал", cb("chan"))]] if posts else None, "overview", topic="matchday")


def tick(now=None) -> dict:
    out = {}
    for name, fn in (("briefing", run_briefing), ("reports", run_reports), ("surges", run_surges),
                     ("not_started", run_not_started), ("preview", run_preview)):
        try:
            out[name] = fn(now)
        except Exception:
            logger.exception("adminbot matchday: %s упал", name)
    return out
