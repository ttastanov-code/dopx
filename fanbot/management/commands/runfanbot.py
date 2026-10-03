"""Бот болельщиков DOPX (long polling).

    python manage.py runfanbot
"""
import logging
import time

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand

from core import heartbeat
from fanbot import bot, services

logger = logging.getLogger("fanbot")
POLL_SECONDS = 25
BOT_DESCRIPTION = ("DOPX. Голос трибун.\n\n"
                   "Оценивайте игроков, тренеров и судей после матчей, делайте прогнозы и смотрите, что думают трибуны. "
                   "Бот присылает уведомления: старт матча вашего клуба, «оцените игроков», итоги прогнозов.\n\n"
                   "Нажмите «Запустить», затем «Открыть DOPX». Аккаунт создастся сам, регистрация не нужна.")


class Command(BaseCommand):
    help = "Telegram-бот для болельщиков: /start, привязка аккаунта, уведомления."
    stats = {"updates": 0, "errors": 0, "last_error": ""}

    def handle(self, *args, **opts):
        if not services.enabled():
            self.stdout.write("Бот болельщиков выключен: нет FAN_BOT_TOKEN. Жду.")
            while True:   # не выходим и отмечаемся — иначе Docker решит, что контейнер завис
                heartbeat.ALIVE_FILE.touch()
                time.sleep(60)
        self.setup_menu()
        self.stdout.write("Бот болельщиков запущен.")
        try:
            while True:
                self.poll_once()
                heartbeat.beat("fan_bot", updates=self.stats["updates"], errors=self.stats["errors"],
                               last_error=self.stats["last_error"])
                if heartbeat.restart_requested("fan_bot"):
                    self.stdout.write("Перезапуск по кнопке из дашборда.")
                    return
        except KeyboardInterrupt:
            self.stdout.write("\nБот болельщиков остановлен.")

    def setup_menu(self):
        """Профиль бота из кода: команды, описания, кнопка меню (Mini App — только на https). Повторять безопасно."""
        try:
            me = services.call("getMe")
        except Exception as e:
            self.stderr.write(f"Токен FAN_BOT_TOKEN не подошёл: {e}")
            return
        if me.get("username", "").lower() != services.bot_username().lower():
            self.stderr.write(f"Внимание: токен от @{me.get('username')}, а FAN_BOT_USERNAME = {services.bot_username()}.")
        steps = [
            ("setMyCommands", {"commands": [
                {"command": "start", "description": "Открыть DOPX"},
                {"command": "notify", "description": "Включить или выключить уведомления"},
                {"command": "stop", "description": "Выключить уведомления"},
            ]}),
            ("setMyShortDescription", {"short_description": "Голос трибун: оценки игроков, прогнозы и уведомления о матчах."}),
            ("setMyDescription", {"description": BOT_DESCRIPTION}),
        ]
        url = services.app_url()
        if url:
            steps.append(("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "DOPX", "web_app": {"url": url}}}))
        for method, params in steps:
            try:
                services.call(method, **params)
            except Exception as e:
                logger.warning("fanbot: %s не прошёл: %s", method, e)

    def poll_once(self):
        offset = cache.get("fanbot:offset")
        params = {"timeout": POLL_SECONDS, "allowed_updates": ["message", "my_chat_member"]}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.post(f"https://api.telegram.org/bot{settings.FAN_BOT_TOKEN}/getUpdates", json=params,
                                 timeout=POLL_SECONDS + 10)
            updates = resp.json().get("result", []) if resp.ok else []
            if resp.status_code == 409:
                # Этот же токен опрашивает другой процесс (обычно прод, а это ноутбук) — ждём, не спорим.
                logger.warning("fanbot: бот уже запущен в другом месте — этот экземпляр ждёт")
                time.sleep(60)
            elif not resp.ok:
                time.sleep(5)
        except requests.RequestException:
            logger.warning("fanbot: нет связи с Telegram")
            time.sleep(5)
            return
        for update in updates:
            cache.set("fanbot:offset", update["update_id"] + 1, None)
            self.stats["updates"] += 1
            try:
                bot.handle(update)
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{time.strftime('%d.%m %H:%M')} {type(e).__name__}: {e}"[:200]
                logger.exception("fanbot: ошибка обработки апдейта")
