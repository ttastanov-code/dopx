# fanbot/bot.py
"""Ответы бота болельщиков: /start (приветствие, привязка по коду), /stop и /notify — уведомления,
/phone и «Поделиться номером» — подтверждение телефона."""
from __future__ import annotations

import html
import logging

from django.conf import settings

from . import services
from .models import TelegramAccount

logger = logging.getLogger(__name__)

WELCOME = ("👋 <b>Это DOPX. Голос трибун.</b>\n\n"
           "После каждого матча болельщики оценивают игроков, тренеров и судей, а до матча делают прогнозы. "
           "Из голосов складываются честные рейтинги.\n\n"
           "<b>Что умеет этот бот</b>\n"
           "• присылает уведомления: старт матча вашего клуба, «пора оценить игроков», итоги ваших прогнозов\n"
           "• открывает DOPX прямо в Telegram, без установки и регистрации\n\n"
           "Команды: /notify включает и выключает уведомления, /stop выключает.")
# Подставляется к приветствию, когда есть кнопка Mini App (сайт на https).
WELCOME_APP = ("\n\n<b>Как начать</b>\nНажмите «⚽ Открыть DOPX» ниже, и аккаунт создастся сам. "
               "Уже есть аккаунт на сайте? Войдите в него на открывшейся странице, и мы объединим аккаунты. Оценки и прогнозы сохранятся.")


def _app_button(start: str = "", label: str = "⚽ Открыть DOPX") -> list | None:
    """Кнопка открыть DOPX: Mini App внутри Telegram (нужен https) или ссылка на сайт."""
    url = services.app_url(start)
    if url:
        return [[{"text": label, "web_app": {"url": url}}]]
    link = services.miniapp_link(start) or settings.SITE_URL.rstrip("/") + services.resolve_start(start)
    return [[{"text": label, "url": link}]] if link.startswith("https://") else None


def _with_channel(rows: list | None) -> list | None:
    """Добавить кнопку канала проекта (если он задан как @имя)."""
    channel = settings.ADMIN_BOT_CHANNEL_ID
    if not str(channel).startswith("@"):
        return rows
    return (rows or []) + [[{"text": "📣 Канал DOPX: новости и разборы матчей", "url": f"https://t.me/{channel[1:]}"}]]


def _no_app_hint() -> str:
    """Без https нет Mini App и кнопки — объясняем, как привязать аккаунт через сайт. Адрес — из SITE_URL."""
    site = settings.SITE_URL.rstrip("/")
    return (f"\n\n<b>Как начать</b>\n1. Войдите на сайте {site}\n2. Профиль → блок «Telegram» → «Привязать Telegram»\n"
            "3. Вернитесь сюда и нажмите «Запустить». После этого начнут приходить уведомления.")


# Кнопка под полем ввода: Telegram сам отправляет номер владельца аккаунта.
PHONE_KEYBOARD = {"keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}]],
                  "resize_keyboard": True, "one_time_keyboard": True}
PHONE_ASK = ("Подтвердите номер телефона: нажмите кнопку «📱 Поделиться номером» внизу.\n"
             "Номер не увидят другие болельщики. Он защищает рейтинг от накрутки: один номер — один аккаунт.")


def reply(chat_id: int, text: str, keyboard=None, markup: dict | None = None) -> None:
    params = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = {"inline_keyboard": keyboard}
    elif markup:
        params["reply_markup"] = markup
    try:
        services.call("sendMessage", **params)
    except Exception as e:
        logger.warning("fanbot: ответ не ушёл: %s", e)


def handle(update: dict) -> None:
    member = update.get("my_chat_member")
    if member and member.get("chat", {}).get("type") != "private":
        # Бот болельщиков живёт только в личке: из групп и каналов выходит сам.
        if member.get("new_chat_member", {}).get("status") in ("member", "administrator"):
            try:
                services.call("leaveChat", chat_id=member["chat"]["id"])
            except Exception as e:
                logger.warning("fanbot: не вышел из чата: %s", e)
        return
    if member:
        # Пользователь заблокировал или снова разрешил бота.
        status = member.get("new_chat_member", {}).get("status")
        TelegramAccount.objects.filter(telegram_id=member.get("chat", {}).get("id")).update(can_message=status == "member")
        return
    msg = update.get("message") or {}
    chat = msg.get("chat", {})
    if chat.get("type") != "private":
        return
    who = msg.get("from", {})
    tid, text = who.get("id"), (msg.get("text") or "").strip()
    if not tid:
        return
    tg = {"id": tid, "username": who.get("username", ""), "first_name": who.get("first_name", ""),
          "last_name": who.get("last_name", "")}
    acc = TelegramAccount.objects.select_related("user").filter(telegram_id=tid).first()
    if msg.get("contact"):
        if not acc:
            return reply(tid, "Сначала привяжите Telegram к аккаунту DOPX: профиль → «Подтвердить номер».",
                         markup={"remove_keyboard": True})
        ok, message = services.save_phone(acc.user, msg["contact"], tid)
        return reply(tid, ("✅ " if ok else "⚠️ ") + html.escape(message),
                     markup={"remove_keyboard": True} if ok else PHONE_KEYBOARD)
    if text.startswith("/start"):
        payload = text.split(maxsplit=1)[1] if " " in text else ""
        if payload == "phone" or payload.startswith("phone_"):
            if payload.startswith("phone_"):
                ok, message = services.link_by_code(payload[6:], tg)
                if not ok:
                    return reply(tid, "⚠️ " + html.escape(message))
                acc = TelegramAccount.objects.select_related("user").filter(telegram_id=tid).first()
            if not acc:
                return reply(tid, "Аккаунт DOPX ещё не привязан." + _no_app_hint(), _app_button())
            return reply(tid, PHONE_ASK, markup=PHONE_KEYBOARD)
        if payload.startswith("m_"):
            return reply(tid, "Матч на DOPX: оценки игроков и прогнозы.", _app_button(payload, "⚽ Открыть матч"))
        if payload.startswith("link_"):
            ok, message = services.link_by_code(payload[5:], tg)
            return reply(tid, ("✅ " if ok else "⚠️ ") + html.escape(message), _app_button())
        if acc:
            TelegramAccount.objects.filter(pk=acc.pk).update(can_message=True)
            phone_hint = "" if acc.user.phone else "\n/phone — подтвердить номер телефона."
            return reply(tid, f"С возвращением, <b>{html.escape(acc.user.username)}</b>! Уведомления будут приходить сюда.\n"
                              "/notify включает и выключает уведомления, /stop выключает." + phone_hint,
                         _with_channel(_app_button()))
        hint = WELCOME_APP if services.app_url() else _no_app_hint()
        return reply(tid, WELCOME + hint, _with_channel(_app_button()))
    if text == "/phone":
        if not acc:
            return reply(tid, "Аккаунт DOPX ещё не привязан." + _no_app_hint(), _app_button())
        return reply(tid, PHONE_ASK, markup=PHONE_KEYBOARD)
    if text == "/stop":
        if acc:
            TelegramAccount.objects.filter(pk=acc.pk).update(notify=False)
        return reply(tid, "🔕 Уведомления выключены. Включить снова: /notify.")
    if text == "/notify":
        if not acc:
            if services.app_url():
                return reply(tid, "Сначала откройте DOPX кнопкой ниже, так появится аккаунт.", _app_button())
            return reply(tid, "Аккаунт DOPX ещё не привязан." + _no_app_hint(), _app_button())
        new = not acc.notify
        TelegramAccount.objects.filter(pk=acc.pk).update(notify=new, can_message=True)
        return reply(tid, "🔔 Уведомления включены." if new else "🔕 Уведомления выключены.")
    reply(tid, WELCOME, _with_channel(_app_button()))
