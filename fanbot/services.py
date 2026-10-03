# fanbot/services.py
"""Вход через Telegram, привязка аккаунта и отправка сообщений болельщикам от их бота."""
from __future__ import annotations

import logging
import re
import time

import requests
from django.conf import settings
from django.contrib.auth import login
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from .models import TelegramAccount, TelegramLinkCode

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
# Адрес-заглушка для аккаунтов без почты: на .invalid письма не отправляются (core.utils.is_placeholder_email).
PLACEHOLDER_DOMAIN = "telegram.invalid"
# Пауза между сообщениями рассылки: лимит Telegram — около 30 в секунду.
SEND_PAUSE = 0.05


def enabled() -> bool:
    return bool(settings.FAN_BOT_TOKEN)


def bot_username() -> str:
    return settings.FAN_BOT_USERNAME.lstrip("@")


def miniapp_link(start: str = "") -> str:
    """Ссылка, открывающая Mini App в Telegram; без настроенного приложения — пусто."""
    if not (bot_username() and settings.FAN_BOT_APP_NAME):
        return ""
    return f"https://t.me/{bot_username()}/{settings.FAN_BOT_APP_NAME}" + (f"?startapp={start}" if start else "")


def app_url(start: str = "") -> str:
    """Адрес Mini App для web_app-кнопки (только https). start — куда перейти после входа."""
    site = settings.SITE_URL.rstrip("/")
    if not site.startswith("https://"):
        return ""
    return f"{site}/tg/app/" + (f"?start={start}" if start else "")


def _username_from(tg: dict) -> str:
    from users.models import User

    base = re.sub(r"[^\w.@+-]", "", tg.get("username") or "")[:24] or f"tg{tg['id']}"
    name, n = base, 2
    while User.objects.filter(username__iexact=name).exists():
        name, n = f"{base}{n}", n + 1
    return name


def account_for(tg: dict) -> TelegramAccount:
    """Аккаунт по данным Telegram: существующий или новый пользователь с подтверждённым статусом."""
    from users.models import User, UserXP

    acc = TelegramAccount.objects.select_related("user").filter(telegram_id=tg["id"]).first()
    if acc is None:
        with transaction.atomic():
            user = User(username=_username_from(tg), email=f"tg{tg['id']}@{PLACEHOLDER_DOMAIN}",
                        first_name=(tg.get("first_name") or "")[:150], last_name=(tg.get("last_name") or "")[:150],
                        is_verified=True)
            user.set_unusable_password()
            user.save()
            UserXP.objects.get_or_create(user=user)
            acc = TelegramAccount.objects.create(user=user, telegram_id=tg["id"])
            acc.created = True  # только что создан — после входа показываем анкету
    acc.username = (tg.get("username") or "")[:64]
    acc.first_name = (tg.get("first_name") or "")[:128]
    acc.photo_url = (tg.get("photo_url") or "")[:500]
    acc.last_seen = timezone.now()
    if tg.get("allows_write"):
        acc.can_message = True
    acc.save(update_fields=["username", "first_name", "photo_url", "last_seen", "can_message"])
    return acc


def login_telegram(request, tg: dict) -> TelegramAccount | None:
    acc = account_for(tg)
    if not acc.user.is_active:
        return None
    login(request, acc.user, backend="django.contrib.auth.backends.ModelBackend")
    from analytics.models import EventName
    from analytics.services import track_event

    track_event(EventName.USER_LOGIN, request=request, properties={"method": "telegram"})
    return acc


def _empty_telegram_only(user) -> bool:
    """Аккаунт создан входом через Telegram и ничего не успел: без почты, пароля, оценок и прогнозов."""
    from evaluations.models import EvaluationSession
    from predictions.models import MatchPrediction

    return (not user.has_real_email and not user.has_usable_password() and not user.is_staff
            and not EvaluationSession.objects.filter(user=user).exists()
            and not MatchPrediction.objects.filter(user=user).exists())


def merge_into(request, target) -> str | None:
    """Перенести Telegram с текущего пустого аккаунта на target (вошли его паролем) и войти в target.
    Возвращает текст ошибки или None."""
    current = request.user
    if target.pk == current.pk:
        return "Это и есть ваш текущий аккаунт."
    if not target.is_active:
        return "Этот аккаунт заблокирован."
    if not _empty_telegram_only(current):
        return "В этом аккаунте уже есть оценки или прогнозы. Автоматически объединить нельзя, напишите нам."
    with transaction.atomic():
        acc = TelegramAccount.objects.select_for_update().get(user=current)
        TelegramAccount.objects.filter(user=target).delete()  # у target был другой Telegram — заменяем
        acc.user = target
        acc.save(update_fields=["user"])
        current.delete()
    login(request, target, backend="django.contrib.auth.backends.ModelBackend")
    return None


def link_by_code(code: str, tg: dict) -> tuple[bool, str]:
    """/start link_<код> в боте: привязать Telegram к аккаунту, где нажали «Привязать»."""
    lc = TelegramLinkCode.objects.select_related("user").filter(code=code).first()
    if not lc or not lc.is_valid():
        return False, "Ссылка устарела. Нажмите «Привязать Telegram» в профиле ещё раз."
    other = TelegramAccount.objects.select_related("user").filter(telegram_id=tg["id"]).exclude(user=lc.user).first()
    if other:
        if not _empty_telegram_only(other.user):
            return False, (f"Этот Telegram уже привязан к аккаунту {other.user.username}, где есть оценки или прогнозы. "
                           "Войдите в него и отвяжите Telegram в профиле или напишите нам, и мы объединим аккаунты.")
        # Пустой аккаунт, который создал вход через Telegram, — убираем, Telegram переходит к основному.
        other.user.delete()
    try:
        acc, _ = TelegramAccount.objects.update_or_create(user=lc.user, defaults={
            "telegram_id": tg["id"], "username": (tg.get("username") or "")[:64],
            "first_name": (tg.get("first_name") or "")[:128], "can_message": True})
    except IntegrityError:
        return False, "Не получилось привязать. Попробуйте ещё раз."
    lc.used_at = timezone.now()
    lc.save(update_fields=["used_at"])
    return True, f"Готово: Telegram привязан к аккаунту {acc.user.username}. Сюда будут приходить уведомления."


# ---------------- Отправка от бота болельщиков
def call(method: str, **params):
    from adminbot.telegram import TelegramError, redact

    try:
        resp = requests.post(API.format(token=settings.FAN_BOT_TOKEN, method=method), json=params, timeout=15)
    except requests.RequestException as e:
        raise type(e)(redact(e)) from None
    data = resp.json()
    if not data.get("ok"):
        raise TelegramError(data.get("error_code", resp.status_code), data.get("description", ""))
    return data["result"]


def send(chat_id: int, text: str, button: tuple[str, str] | None = None) -> bool:
    """Сообщение болельщику. Кнопка (текст, url) — ссылка на сайт или Mini App."""
    from adminbot.telegram import TelegramError

    params = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if button and button[1].startswith("https://"):
        text_, url = button
        # Свой /tg/app/ — открываем Mini App прямо из сообщения, без /newapp.
        btn = {"text": text_, "web_app": {"url": url}} if app_url() and url.startswith(app_url()) else {"text": text_, "url": url}
        params["reply_markup"] = {"inline_keyboard": [[btn]]}
    try:
        call("sendMessage", **params)
        return True
    except TelegramError as e:
        # Пользователь заблокировал бота или не запускал его — больше не пишем.
        if e.code in (400, 403) and ("blocked" in e.description or "chat not found" in e.description or "deactivated" in e.description):
            TelegramAccount.objects.filter(telegram_id=chat_id).update(can_message=False)
        else:
            logger.warning("fanbot: сообщение не ушло: %s", e.description)
    except requests.RequestException as e:
        logger.warning("fanbot: сеть: %s", e)
    return False


def notify_users(user_ids, title: str, body: str, url: str) -> int:
    """Уведомление в Telegram тем, кто привязал бота и не выключил уведомления. Вызывает push-рассылка."""
    from core.safe_mode import allowed

    if not enabled() or not allowed("push"):
        return 0
    import html

    accounts = list(TelegramAccount.objects.filter(user_id__in=list(user_ids), can_message=True, notify=True,
                                                   user__is_active=True).values_list("telegram_id", flat=True))
    if not accounts:
        return 0
    full_url = url if url.startswith("http") else settings.SITE_URL.rstrip("/") + url
    text = f"<b>{html.escape(title)}</b>\n{html.escape(body)}"
    param = _start_param(url)
    button = ("Открыть", miniapp_link(param) or app_url(param) or full_url)
    sent = 0
    for chat_id in accounts:
        sent += send(chat_id, text, button)
        time.sleep(SEND_PAUSE)
    return sent


def _start_param(url: str) -> str:
    """Путь сайта → параметр Mini App: /matches/<uuid>/… → m_<uuid>, иначе путь как есть (если влезает)."""
    m = re.search(r"/matches/([0-9a-f-]{36})", url or "")
    if m:
        return f"m_{m.group(1)}"
    return ""


def resolve_start(param: str) -> str:
    """Параметр Mini App → путь на сайте."""
    m = re.fullmatch(r"m_([0-9a-f-]{36})", param or "")
    if m:
        return reverse("matches:detail", args=[m.group(1)])
    return reverse("core:home")
