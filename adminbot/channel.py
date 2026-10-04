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
    "preview": "за сутки до тура: что на кону, форма, прогнозы болельщиков",
    "result": "сразу после финального свистка: счёт, голы, призыв оценить",
    "ratings": "через 4 часа после матча: мини-статья — история, цифры, герой и антигерой",
    "round": "когда тур закрыт: альбом карточек и итоговый текст",
    "poll": "опросы к превью и итогам тура: «Кто выиграет?», «Лучший игрок тура»",
    "controversy": "по вторникам: спорный судья тура и опрос",
    "number": "по средам: рубрика «Цифра недели»",
    "expert": "когда мнение эксперта опубликовано",
    "changes": "перенос или отмена матча",
}
QUIET_FROM, QUIET_TO = 23, 9
HASHTAGS = "#DOPX #КПЛ"


def channel_id() -> str:
    return settings.ADMIN_BOT_CHANNEL_ID


def configured() -> bool:
    from core.safe_mode import allowed

    return bool(channel_id()) and tg.enabled() and allowed("channel")


def site(path: str) -> str:
    return settings.ADMIN_BOT_PUBLIC_URL.rstrip("/") + path


def match_url(match) -> str:
    """Матч: в Mini App бота болельщиков (оценка внутри Telegram), если оно настроено, иначе на сайте."""
    from fanbot.services import miniapp_link

    return miniapp_link(f"m_{match.pk}") or site(reverse("matches:detail", args=[match.pk]))


def fan_bot_url(start: str = "channel") -> str:
    """DOPX в Telegram: сразу Mini App (Direct Link, FAN_BOT_APP_NAME), иначе чат бота. Пусто — бот не настроен."""
    from fanbot.services import bot_username, enabled, miniapp_link
    if not (enabled() and bot_username()):
        return ""
    return miniapp_link(start) or f"https://t.me/{bot_username()}?start={start}"


def bot_line() -> str:
    """Строка про DOPX в Telegram в анонсе и итогах тура."""
    from fanbot.services import bot_username
    if not fan_bot_url():
        return ""
    return (f"\n\n📱 Оценки и прогнозы прямо в Telegram, без регистрации: @{bot_username()}. "
            "Там же напоминания о матчах вашего клуба.")


def bot_intro_post(user=None) -> ChannelPost:
    """Черновик «знакомство с ботом болельщиков» для канала; публикуют и закрепляют из дашборда."""
    from fanbot.services import bot_username

    text = ("📱 <b>DOPX теперь прямо в Telegram</b>\n\n"
            "Весь сайт открывается внутри Telegram: оценивайте игроков, тренеров и судей после матча, "
            "делайте прогнозы, смотрите рейтинги и таблицы. Регистрация не нужна, вход по Telegram в одно касание.\n\n"
            f"Бот @{bot_username()} ещё и напомнит лично вам:\n"
            "• когда начинается матч вашего клуба\n"
            "• когда пора оценить игроков после финального свистка\n"
            "• чем закончились ваши прогнозы\n\n"
            "Нажмите кнопку ниже.")
    return ChannelPost.objects.create(kind="manual", text=text, buttons=[["📱 Открыть DOPX в Telegram", fan_bot_url("intro")]],
                                      created_by=user)


def pin(post: ChannelPost) -> tuple[bool, str]:
    """Закрепить опубликованный пост в канале (бот должен быть админом с правом редактирования)."""
    if post.status != "published" or not post.message_id:
        return False, "Закрепить можно только опубликованный пост."
    try:
        tg.call("pinChatMessage", chat_id=channel_id(), message_id=post.message_id, disable_notification=True)
    except tg.TelegramError as e:
        return False, f"Telegram не дал закрепить: {e.description}. Проверьте, что у бота есть право закреплять сообщения."
    return True, "Пост закреплён в канале."


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
def prepare(kind: str, key: str, text, image: str = "", buttons=None, sample: bool = False,
            images=None, poll=None, delay: int = 0) -> ChannelPost | None:
    """Пост из события: ключ от дублей, режим типа — сразу, на одобрение или никак.
    text может быть функцией → (текст, by_ai): ИИ зовём, только если пост нужен; sample — пробный черновик."""
    if not sample:
        if not configured():
            return None
        cfg = ChannelConfig.get()
        mode = cfg.mode(kind)
        if mode == "off" or ChannelPost.objects.filter(key=key).exists():
            return None
    by_ai = False
    if callable(text):
        text, by_ai = text()
    text = with_link(text, buttons) if not poll else text
    fields = dict(kind=kind, text=text, image=image, buttons=buttons or [], images=images or [], poll=poll, by_ai=by_ai)
    if sample:
        return ChannelPost.objects.create(key=f"sample:{timezone.now().timestamp()}:{key}"[:120], **fields)
    post = ChannelPost(key=key, **fields)
    if mode == "auto":
        now = timezone.now() + timedelta(seconds=delay)
        post.status = "scheduled"
        post.scheduled_at = _after_quiet(now) if cfg.quiet_hours else now
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
    note = header() + f"📣 <b>Пост в канал на одобрение</b> · {esc(post.get_kind_display())}" + (" · ✍️ Claude" if post.by_ai else "") + "\n\n"
    if post.poll:
        note += f"📊 Опрос: <b>{esc(post.poll['question'])}</b>\n" + "\n".join(f"• {esc(o)}" for o in post.poll["options"])
        for link in links_for("channel", "adminbot.view_channelpost", topic="channel"):
            tg.send(link.telegram_id, note, rows)
        return
    if post.images:
        note += f"🖼 Альбом: {len(post.images) + bool(post.image)} картинки\n\n"
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
def _read(path: str) -> bytes:
    from django.core.files.storage import default_storage

    with default_storage.open(path, "rb") as fh:
        return fh.read()


def _image_bytes(post) -> bytes:
    return _read(post.image)


def _button_rows(post) -> list:
    rows = [[(b[0], b[1])] for b in post.buttons if len(b) == 2 and str(b[1]).startswith("http")]
    # Под каждым постом кнопка DOPX внутри Telegram (Mini App или чат бота).
    from fanbot.services import bot_username
    url = fan_bot_url("channel")
    if url and not any("t.me/" + bot_username() in str(b[1]) for r in rows for b in r):
        rows.append([("📱 DOPX в Telegram: оценки и прогнозы", url)])
    return rows


def _send(post) -> dict:
    """Отправка в канал по виду поста: опрос, альбом, фото с текстом или текст."""
    rows = _button_rows(post)
    if post.poll:
        return tg.call("sendPoll", chat_id=channel_id(), question=post.poll["question"][:300],
                       options=[{"text": o[:100]} for o in post.poll["options"][:10]], is_anonymous=True)
    album = ([post.image] if post.image else []) + list(post.images)
    if len(album) > 1:
        files = {f"p{n}": (f"p{n}.png", _read(path)) for n, path in enumerate(album[:10])}
        media = [{"type": "photo", "media": f"attach://p{n}"} for n in range(len(files))]
        caption_fits = len(post.text) <= 1024 and not rows
        if caption_fits:
            media[0].update(caption=post.text, parse_mode="HTML")
        msgs = tg.call_files("sendMediaGroup", files, chat_id=channel_id(), media=media)
        return msgs[0] if caption_fits else tg.post(channel_id(), post.text, rows)
    if post.image or post.tg_file_id:
        photo = post.tg_file_id or _image_bytes(post)
        if len(post.text) <= 1024:
            return tg.send_photo(channel_id(), photo, post.text, rows)
        tg.send_photo(channel_id(), photo)
    return tg.post(channel_id(), post.text, rows)


def publish(post: ChannelPost, user=None) -> tuple[bool, str]:
    """Опубликовать сейчас. Длинный текст к картинке уходит отдельным сообщением (лимит подписи 1024)."""
    if not configured():
        return False, "Канал пока не подключён — пост сохранён, опубликовать можно позже."
    claimed = ChannelPost.objects.filter(pk=post.pk, status__in=["draft", "scheduled", "failed"]).update(status="published")
    if not claimed:
        return False, "Пост уже опубликован или удалён."
    try:
        msg = _send(post)
    except (tg.TelegramError, requests.RequestException, OSError) as e:
        error = tg.redact(e)
        ChannelPost.objects.filter(pk=post.pk).update(status="failed", error=error[:300])
        logger.warning("adminbot: публикация в канал не удалась: %s", error)
        return False, f"Telegram не принял пост: {error[:150]}"
    ChannelPost.objects.filter(pk=post.pk).update(published_at=timezone.now(), message_id=msg.get("message_id"),
                                                  published_by=user, error="")
    return True, "📣 Опубликовано в канале."


def publish_due() -> int:
    n = 0
    for post in ChannelPost.objects.filter(status="scheduled", scheduled_at__lte=timezone.now()).order_by("scheduled_at", "pk"):
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
    from .stories import GOAL_TYPES, _events

    until = timezone.localtime(match.voting_open_until)
    goals = [e for e in _events(match) if e.event_type in GOAL_TYPES]
    scorers = ", ".join(f"{e.player_display_name or ''} {e.display_minute}'".strip() for e in goals)
    text = (f"🏁 <b>Финал. {esc(_score(match))}</b>\n\n" + (f"⚽ {esc(scorers)}\n\n" if scorers else "")
            + f"Как сыграли игроки, тренеры и судья? Оценки болельщиков открыты до {until:%d.%m %H:%M} — "
            f"разбор матча по вашим голосам выйдет позже.\n\n{HASHTAGS}")
    return prepare("result", f"result:{match.pk}", text, buttons=[["⭐ Оценить матч", match_url(match)]], sample=sample)


def review_post(match, sample: bool = False) -> ChannelPost | None:
    """Разбор матча: мини-статья (Claude или шаблон) + карточка героя."""
    from core.services.share_cards import build_social_card

    from . import stories, writer

    facts = stories.match_facts(match)
    if not facts.get("лучшие") and not facts.get("голы"):
        return None
    image = ""
    if facts.get("лучшие"):
        hero = facts["лучшие"][0]
        try:
            from aggregates.models import PlayerMatchAggregate
            from core.cards import player_photo

            # Фото героя — не в facts: их читает ИИ, ссылка ему не нужна.
            top = PlayerMatchAggregate.objects.filter(match=match).select_related("player").order_by("-performance_score").first()
            image = build_social_card(kind="player_of_round", eyebrow="ГЕРОЙ МАТЧА ПО МНЕНИЮ БОЛЕЛЬЩИКОВ",
                                      number_text=f"{hero['оценка']:.1f}", label_line1=hero["игрок"],
                                      label_line2=f"{match.home_team.name} {match.home_score}:{match.away_score} {match.away_team.name}",
                                      image_url=player_photo(top.player) if top and top.player.full_name == hero["игрок"] else "")
        except Exception:
            logger.warning("adminbot: карточка героя не собралась", exc_info=True)

    def text():
        body, by_ai = writer.write("review", facts, lambda: stories.review_template(match, facts))
        return f"{body}\n\n{HASHTAGS}", by_ai
    return prepare("ratings", f"ratings:{match.pk}", text, image=image, buttons=[["⭐ Все оценки матча", match_url(match)]],
                   sample=sample)


def ratings_post(match, best=None, worst=None, votes=None, sample: bool = False) -> ChannelPost | None:
    return review_post(match, sample=sample)


def preview_post(tour, matches: list, sample: bool = False) -> ChannelPost | None:
    from . import stories, writer

    key = f"preview:{matches[0].season_id}:{tour}" if tour else f"preview:{matches[0].pk}"
    facts, main = stories.preview_facts(tour, matches)

    def text():
        body, by_ai = writer.write("preview", facts, lambda: stories.preview_template(facts))
        return f"{body}{bot_line()}\n\n{HASHTAGS}", by_ai
    post = prepare("preview", key, text, buttons=[["🔮 Сделать прогноз", site(reverse("matches:list"))]], sample=sample)
    if post and main:
        prepare("poll", f"poll:{key}", f"Опрос: кто выиграет {main.home_team.name} – {main.away_team.name}?",
                poll={"question": f"Кто выиграет: {main.home_team.name} – {main.away_team.name}?",
                      "options": [main.home_team.name, "Ничья", main.away_team.name]}, sample=sample, delay=60)
    return post


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


def round_posts(sample: bool = False, rnd=None) -> list[ChannelPost]:
    """Итоги тура: альбом карточек (как в «Соцсетях») + итоговый текст, потом опрос «Лучший игрок тура»."""
    from django.core.files.storage import default_storage

    from engagement.social import weekly_content

    from . import stories, writer

    rnd = rnd or stories.latest_final_round() or _any_round()
    if rnd is None:
        return []
    data = weekly_content() or {"posts": []}
    base = default_storage.url("")
    paths = [p["image"][len(base):] for p in data["posts"] if p["image"].startswith(base)][:4]
    facts = stories.round_facts(rnd)
    if not stories.round_has_story(facts):
        return []

    def text():
        body, by_ai = writer.write("round", facts, lambda: stories.round_template(facts))
        return f"{body}{bot_line()}\n\n{HASHTAGS}", by_ai
    key = f"round:{rnd.season_id}:{rnd.tour}"
    post = prepare("round", key, text, image=paths[0] if paths else "", images=paths[1:],
                   buttons=[["🏆 Лучшие тура на DOPX", site("/")]], sample=sample)
    out = [post] if post else []
    poll = stories.round_poll(facts)
    if post and poll:
        p = prepare("poll", f"poll:{key}", poll["question"], poll=poll, sample=sample, delay=60)
        out += [p] if p else []
    return out


def _any_round():
    from round_squad.models import RoundBestXI

    return RoundBestXI.objects.filter(is_final=True).select_related("season").order_by("-season__year", "-tour").first()


def controversy_post(sample: bool = False) -> list[ChannelPost]:
    from . import stories, writer

    rnd = stories.latest_final_round() if not sample else (stories.latest_final_round() or _any_round())
    facts = stories.controversy_facts(rnd) if rnd else None
    if not facts:
        return []

    def text():
        body, by_ai = writer.write("controversy", facts, lambda: stories.controversy_template(facts), "400–800 знаков")
        return f"{body}\n\n{HASHTAGS}", by_ai
    key = f"controversy:{rnd.season_id}:{rnd.tour}"
    post = prepare("controversy", key, text, sample=sample)
    out = [post] if post else []
    if post:
        poll = stories.controversy_poll(facts)
        p = prepare("poll", f"poll:{key}", poll["question"], poll=poll, sample=sample, delay=60)
        out += [p] if p else []
    return out


def number_post(sample: bool = False) -> ChannelPost | None:
    from core.services.share_cards import build_social_card

    from . import stories, writer

    rnd = stories.latest_final_round() if not sample else (stories.latest_final_round() or _any_round())
    facts = stories.number_facts(rnd) if rnd else None
    if not facts:
        return None
    try:
        image = build_social_card(kind="drama", eyebrow="ЦИФРА НЕДЕЛИ · DOPX", number_text=str(facts["цифра"]),
                                  label_line1=facts["тема"].capitalize(), label_line2=facts.get("игрок") or f"{facts['тур']}-й тур")
    except Exception:
        image = ""

    def text():
        body, by_ai = writer.write("number", facts, lambda: stories.number_template(facts), "300–600 знаков")
        return f"{body}\n\n{HASHTAGS}", by_ai
    return prepare("number", f"number:{rnd.season_id}:{rnd.tour}:{facts['тема']}", text, image=image,
                   buttons=[["⭐ Оценить матчи тура", site(reverse("matches:list"))]], sample=sample)


def weekly_rubric(now=None) -> list:
    """Рубрики по дням: вторник — спорный момент, среда — цифра недели."""
    day = timezone.localtime(now or timezone.now()).isoweekday()
    if day == 2:
        return controversy_post()
    if day == 3:
        post = number_post()
        return [post] if post else []
    return []


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
    """По посту каждого формата из настоящих данных — черновиками, чтобы посмотреть, как выглядят."""
    from matches.models import Match
    from engagement.models import ExpertTake

    out: list = []
    upcoming = list(Match.objects.filter(status="scheduled", start_time__gt=timezone.now())
                    .select_related("home_team", "away_team").order_by("start_time")[:8])
    if upcoming:
        first = upcoming[0]
        matches = [m for m in upcoming if m.tour == first.tour and m.season_id == first.season_id] if first.tour else upcoming[:5]
        out.append(preview_post(first.tour, matches, sample=True))
    last = Match.objects.filter(status="finished").select_related("home_team", "away_team").order_by("-start_time").first()
    if last:
        out.append(result_post(last, sample=True))
        out.append(review_post(last, sample=True))
    moved = Match.objects.filter(status__in=["postponed", "cancelled"]).select_related("home_team", "away_team").order_by("-start_time").first()
    if moved:
        out.append(change_post(moved, moved.status, sample=True))
    take = ExpertTake.objects.filter(is_published=True).select_related("match__home_team", "match__away_team", "expert").order_by("-created_at").first()
    if take:
        out.append(expert_post(take, sample=True))
    out += round_posts(sample=True)
    out += controversy_post(sample=True)
    out.append(number_post(sample=True))
    return [p for p in out if p]


def unpublish(post: ChannelPost) -> tuple[bool, str]:
    """Удалить опубликованный пост из канала (бот — админ с правом удалять)."""
    if post.status != "published":
        return False, "Пост не опубликован."
    if not post.message_id:
        ChannelPost.objects.filter(pk=post.pk).update(status="cancelled", error="убран из списка")
        return True, "Убрал из списка. Если пост ещё в канале — удалите его там вручную."
    try:
        tg.call("deleteMessage", chat_id=channel_id(), message_id=post.message_id)
    except tg.TelegramError as e:
        if "not found" not in e.description.lower():
            return False, f"Telegram не удалил пост: {e.description[:150]}"
        ChannelPost.objects.filter(pk=post.pk).update(status="cancelled", error="удалён в канале вручную")
        return True, "В канале его уже нет — убрал из списка."
    except requests.RequestException:
        return False, "Нет связи с Telegram — попробуйте ещё раз."
    ChannelPost.objects.filter(pk=post.pk).update(status="cancelled", error="удалён из канала")
    return True, "🗑 Пост удалён из канала."
