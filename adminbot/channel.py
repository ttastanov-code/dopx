# adminbot/channel.py
"""Telegram-канал проекта: бот готовит посты из событий сайта, сотрудники одобряют (или автопостинг)."""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from datetime import timezone as dt_timezone

import requests
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from . import telegram as tg
from .models import ChannelConfig, ChannelPost

logger = logging.getLogger(__name__)

MODES = [("auto", "Сразу"), ("approve", "С одобрением"), ("off", "Не готовить")]
KIND_HINTS = {
    "preview": "за сутки до первого матча тура",
    "result": "после финального свистка: счёт и призыв оценить",
    "ratings": "через несколько часов после матча: лучшие и антигерой",
    "round": "когда тур закрыт: карточки «Игрок тура», «Спорный судья» и др.",
    "expert": "когда мнение эксперта опубликовано",
    "changes": "перенос или отмена матча",
}
QUIET_FROM, QUIET_TO = 23, 9
HASHTAGS = "#DOPX #КПЛ"


def channel_id() -> str:
    return settings.ADMIN_BOT_CHANNEL_ID


def configured() -> bool:
    return bool(channel_id()) and tg.enabled()


def site(path: str) -> str:
    return settings.ADMIN_BOT_PUBLIC_URL.rstrip("/") + path


def match_url(match) -> str:
    return site(reverse("matches:detail", args=[match.pk]))


def esc(s) -> str:
    import html

    return html.escape(str(s))


def _after_quiet(now: datetime) -> datetime:
    """Ближайшее время вне тихих часов (09:00 локально), если сейчас ночь."""
    local = timezone.localtime(now)
    if QUIET_TO <= local.hour < QUIET_FROM:
        return now
    day = local.date() + (timedelta(days=1) if local.hour >= QUIET_FROM else timedelta())
    return timezone.make_aware(datetime.combine(day, time(QUIET_TO)))


def morning(now: datetime | None = None) -> datetime:
    """Завтра (или сегодня, если ещё рано) в 10:00."""
    local = timezone.localtime(now or timezone.now())
    day = local.date() + (timedelta() if local.hour < 10 else timedelta(days=1))
    return timezone.make_aware(datetime.combine(day, time(10)))


# ---------------- Подготовка
def prepare(kind: str, key: str, text: str, image: str = "", buttons=None, sample: bool = False) -> ChannelPost | None:
    """Пост из события. Ключ не даёт продублировать; режим типа решает: сразу, на одобрение или никак.
    sample — пробный черновик: без режимов, уведомлений и проверки канала."""
    text = with_link(text, buttons)
    if sample:
        return ChannelPost.objects.create(kind=kind, key=f"sample:{timezone.now().timestamp()}:{key}"[:120], text=text,
                                          image=image, buttons=buttons or [])
    if not configured():
        return None
    cfg = ChannelConfig.get()
    mode = cfg.mode(kind)
    if mode == "off" or ChannelPost.objects.filter(key=key).exists():
        return None
    post = ChannelPost(kind=kind, key=key, text=text, image=image, buttons=buttons or [])
    if mode == "auto":
        post.status = "scheduled"
        post.scheduled_at = _after_quiet(timezone.now()) if cfg.quiet_hours else timezone.now()
    post.save()
    if mode == "approve":
        transaction.on_commit(lambda: notify_draft(post.pk))
    return post


def with_link(text: str, buttons) -> str:
    """Ссылка из первой кнопки — ещё и в тексте: её видно в превью и при пересылке."""
    if not buttons or "<a href" in text:
        return text
    label, url = buttons[0]
    line = f'👉 <a href="{esc(url)}">{esc(label)}</a>'
    body, sep, tags = text.rpartition("\n\n")
    # Хештеги оставляем последней строкой.
    if sep and tags.startswith("#"):
        return f"{body}\n\n{line}\n\n{tags}"
    return f"{text}\n\n{line}"


def notify_draft(post_id) -> None:
    from .handlers import cb, header
    from .notify import links_for

    post = ChannelPost.objects.filter(pk=post_id).first()
    if not post:
        return
    rows = draft_rows(post, cb)
    note = header() + f"📣 <b>Пост в канал на одобрение</b> · {esc(post.get_kind_display())}\n\n"
    for link in links_for("channel", "adminbot.view_channelpost", topic="channel"):
        if post.image:
            try:
                tg.send_photo(link.telegram_id, _image_bytes(post), note + post.text[:800], rows)
                continue
            except Exception:
                logger.warning("adminbot: превью картинки не отправилось", exc_info=True)
        tg.send(link.telegram_id, note + post.text, rows)


def draft_rows(post, cb) -> list:
    pk = str(post.pk)
    return [
        [("✅ В канал", cb("post_pub", pk)), ("🕙 Утром в 10", cb("post_morning", pk))],
        [("✏️ Изменить текст", cb("post_edit", pk)), ("🗑 Удалить", cb("post_del", pk))],
    ]


# ---------------- Публикация
def _image_bytes(post) -> bytes:
    from django.core.files.storage import default_storage

    with default_storage.open(post.image, "rb") as fh:
        return fh.read()


def _button_rows(post) -> list:
    return [[(b[0], b[1])] for b in post.buttons if len(b) == 2 and str(b[1]).startswith("http")]


def publish(post: ChannelPost, user=None) -> tuple[bool, str]:
    """Опубликовать сейчас. Длинный текст к картинке уходит отдельным сообщением (лимит подписи 1024)."""
    if not configured():
        return False, "Канал пока не подключён — пост сохранён, опубликовать можно позже."
    claimed = ChannelPost.objects.filter(pk=post.pk, status__in=["draft", "scheduled", "failed"]).update(status="published")
    if not claimed:
        return False, "Пост уже опубликован или удалён."
    rows = _button_rows(post)
    try:
        if post.image or post.tg_file_id:
            photo = post.tg_file_id or _image_bytes(post)
            if len(post.text) <= 1024:
                msg = tg.send_photo(channel_id(), photo, post.text, rows)
            else:
                tg.send_photo(channel_id(), photo)
                msg = tg.post(channel_id(), post.text, rows)
        else:
            msg = tg.post(channel_id(), post.text, rows)
    except (tg.TelegramError, requests.RequestException, OSError) as e:
        ChannelPost.objects.filter(pk=post.pk).update(status="failed", error=str(e)[:300])
        logger.warning("adminbot: публикация в канал не удалась: %s", e)
        return False, f"Telegram не принял пост: {str(e)[:150]}"
    ChannelPost.objects.filter(pk=post.pk).update(published_at=timezone.now(), message_id=msg.get("message_id"),
                                                  published_by=user, error="")
    return True, "📣 Опубликовано в канале."


def publish_due() -> int:
    n = 0
    for post in ChannelPost.objects.filter(status="scheduled", scheduled_at__lte=timezone.now()):
        ok, _msg = publish(post)
        n += ok
    return n


# ---------------- Тексты постов
def kickoff(m, fmt: str = "%d.%m %H:%M") -> str:
    """Время начала; полночь по UTC — заглушка поставщика (время ещё не назначено), показываем только дату."""
    utc = m.start_time.astimezone(dt_timezone.utc)
    local = timezone.localtime(m.start_time)
    if utc.hour == 0 and utc.minute == 0:
        return local.strftime(fmt.split(" ")[0]) + " (время уточняется)"
    return local.strftime(fmt)


def _score(m) -> str:
    return f"{m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}"


def result_post(match, sample: bool = False) -> ChannelPost | None:
    until = timezone.localtime(match.voting_open_until)
    text = (f"🏁 <b>Финал. {esc(_score(match))}</b>\n\n"
            f"Как сыграли игроки, тренеры и судья? Оценки болельщиков открыты до {until:%d.%m %H:%M}.\n\n{HASHTAGS}")
    return prepare("result", f"result:{match.pk}", text, buttons=[["⭐ Оценить матч", match_url(match)]], sample=sample)


def ratings_post(match, best: list, worst, votes: int, sample: bool = False) -> ChannelPost | None:
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📊 <b>{esc(_score(match))}: оценки болельщиков</b>", f"Голосов: {votes}", ""]
    lines += [f"{medals[i]} {esc(a.player.full_name)} — {a.performance_score:.1f}" for i, a in enumerate(best[:3])]
    if worst is not None:
        lines += ["", f"🥶 Антигерой: {esc(worst.player.full_name)} — {worst.performance_score:.1f}"]
    lines += ["", "Согласны? Ваш голос меняет рейтинг.", HASHTAGS]
    return prepare("ratings", f"ratings:{match.pk}", "\n".join(lines), buttons=[["Все оценки матча", match_url(match)]], sample=sample)


def preview_post(tour, matches: list, sample: bool = False) -> ChannelPost | None:
    days = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    lines = [f"🗓 <b>{tour}-й тур КПЛ</b>" if tour else "🗓 <b>Ближайшие матчи КПЛ</b>", ""]
    for m in matches:
        t = timezone.localtime(m.start_time)
        lines.append(f"{days[t.weekday()]} {kickoff(m)} · {esc(m.home_team.name)} – {esc(m.away_team.name)}")
    lines += ["", "После финального свистка оценивайте игроков, тренеров и судей на DOPX.", HASHTAGS]
    key = f"preview:{matches[0].season_id}:{tour}" if tour else f"preview:{matches[0].pk}"
    return prepare("preview", key, "\n".join(lines), buttons=[["Календарь и прогнозы", site(reverse("matches:list"))]], sample=sample)


def change_post(match, kind: str, sample: bool = False) -> ChannelPost | None:
    what = {"postponed": "перенесён", "cancelled": "отменён"}.get(kind, "перенесён")
    text = f"⚠️ Матч {esc(match.home_team.name)} – {esc(match.away_team.name)} {what}.\n\nСледите за новой датой на DOPX."
    return prepare("changes", f"change:{match.pk}:{kind}", text, buttons=[["Матч на DOPX", match_url(match)]], sample=sample)


def expert_post(take, sample: bool = False) -> ChannelPost | None:
    m = take.match
    quote = take.headline or take.text[:200]
    body = take.text if len(take.text) <= 600 else take.text[:600].rsplit(" ", 1)[0] + "…"
    title = f", {esc(take.display_title)}" if take.display_title else ""
    text = (f"🎙 <b>Мнение эксперта · {esc(m.home_team.name)} – {esc(m.away_team.name)}</b>\n\n"
            f"<b>«{esc(quote)}»</b>\n\n{esc(body) if take.headline else ''}\n\n— {esc(take.display_name)}{title}\n\n{HASHTAGS}")
    return prepare("expert", f"expert:{take.pk}", text.replace("\n\n\n\n", "\n\n"), buttons=[["Читать на DOPX", match_url(m)]], sample=sample)


def round_posts(sample: bool = False) -> list[ChannelPost]:
    """Карточки итогов последнего тура (те же, что в «Соцсетях»)."""
    from django.core.files.storage import default_storage

    from engagement.social import weekly_content

    data = weekly_content()
    if not data:
        return []
    out = []
    base = default_storage.url("")
    for p in data["posts"]:
        path = p["image"][len(base):] if p["image"].startswith(base) else ""
        post = prepare("round", f"round:{data['title']}:{p['key']}", esc(p["caption"]), image=path,
                       buttons=[["Лучшие тура на DOPX", site("/")]], sample=sample)
        if post:
            out.append(post)
    return out


# ---------------- Разметка из дашборда
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote", "tg-spoiler"}


def clean_html(text: str) -> str:
    """Оставляет теги, которые понимает Telegram; остальное экранирует. Незакрытые теги закрывает."""
    import html
    from html.parser import HTMLParser

    out, stack = [], []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in ALLOWED_TAGS:
                out.append(html.escape(self.get_starttag_text() or ""))
                return
            if tag == "a":
                href = dict(attrs).get("href", "")
                if not href.startswith(("http://", "https://", "tg://")):
                    out.append(html.escape(self.get_starttag_text() or ""))
                    return
                out.append(f'<a href="{html.escape(href, quote=True)}">')
            else:
                out.append(f"<{tag}>")
            stack.append(tag)

        def handle_endtag(self, tag):
            if tag in stack:
                while stack:
                    t = stack.pop()
                    out.append(f"</{t}>")
                    if t == tag:
                        break
            else:
                out.append(html.escape(f"</{tag}>"))

        def handle_data(self, data):
            out.append(html.escape(data, quote=False))

        def handle_entityref(self, name):
            out.append(f"&{name};" if name in ("amp", "lt", "gt", "quot") else html.escape(f"&{name};"))

        def handle_charref(self, name):
            out.append(f"&#{name};")

    p = P(convert_charrefs=False)
    p.feed(text.replace("\r\n", "\n"))
    p.close()
    out += [f"</{t}>" for t in reversed(stack)]
    return "".join(out)


def parse_buttons(raw: str) -> list:
    """Строки «Текст | https://…» → [[текст, url]]."""
    out = []
    for line in (raw or "").splitlines():
        label, _, url = line.partition("|")
        if label.strip() and url.strip().startswith(("http://", "https://")):
            out.append([label.strip()[:40], url.strip()])
    return out[:4]


def save_upload(upload) -> str:
    import uuid

    from django.core.files.storage import default_storage

    ext = (upload.name.rsplit(".", 1)[-1] if "." in upload.name else "jpg").lower()[:5]
    return default_storage.save(f"channel/{uuid.uuid4().hex}.{ext}", upload)


def sample_posts() -> list[ChannelPost]:
    """По посту каждого типа из настоящих данных — черновиками, чтобы посмотреть, как выглядят."""
    from aggregates.models import PlayerMatchAggregate
    from engagement.models import ExpertTake
    from matches.models import Match

    out = []
    upcoming = list(Match.objects.filter(status="scheduled", start_time__gt=timezone.now())
                    .select_related("home_team", "away_team").order_by("start_time")[:8])
    if upcoming:
        first = upcoming[0]
        matches = [m for m in upcoming if m.tour == first.tour and m.season_id == first.season_id] if first.tour else upcoming[:5]
        out.append(preview_post(first.tour, matches, sample=True))
    last = Match.objects.filter(status="finished").select_related("home_team", "away_team").order_by("-start_time").first()
    if last:
        out.append(result_post(last, sample=True))
    # Перенос — только на настоящем перенесённом матче: иначе пробный пост был бы неправдой.
    moved = Match.objects.filter(status__in=["postponed", "cancelled"]).select_related("home_team", "away_team").order_by("-start_time").first()
    if moved:
        out.append(change_post(moved, moved.status, sample=True))
    rated = (PlayerMatchAggregate.objects.filter(total_votes__gt=0, match__status="finished")
             .order_by("-match__start_time").values_list("match_id", flat=True).first())
    if rated:
        m = Match.objects.select_related("home_team", "away_team").get(pk=rated)
        aggs = list(PlayerMatchAggregate.objects.filter(match=m, total_votes__gt=0).select_related("player").order_by("-performance_score"))
        from evaluations.models import EvaluationSession
        votes = EvaluationSession.objects.filter(match=m, status="completed").count()
        out.append(ratings_post(m, aggs[:3], aggs[-1] if len(aggs) > 3 else None, votes, sample=True))
    take = ExpertTake.objects.filter(is_published=True).select_related("match__home_team", "match__away_team", "expert").order_by("-created_at").first()
    if take:
        out.append(expert_post(take, sample=True))
    out += round_posts(sample=True)[:2]
    return [p for p in out if p]


def unpublish(post: ChannelPost) -> tuple[bool, str]:
    """Удалить опубликованный пост из канала (бот — админ с правом удалять)."""
    if post.status != "published" or not post.message_id:
        return False, "Пост не опубликован ботом — удалите его в канале вручную."
    try:
        tg.call("deleteMessage", chat_id=channel_id(), message_id=post.message_id)
    except (tg.TelegramError, requests.RequestException) as e:
        return False, f"Telegram не удалил пост: {str(e)[:150]}. Удалите его в канале вручную."
    ChannelPost.objects.filter(pk=post.pk).update(status="cancelled", error="удалён из канала")
    return True, "🗑 Пост удалён из канала."
