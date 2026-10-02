"""Telegram-бот администрирования.

    python manage.py runbot            # роль выбирается сама
    python manage.py runbot --role listener|agent

Прод всегда «слушатель»: получает апдейты от Telegram. Ноутбук слушает сам, пока прод не отвечает;
как только на ADMIN_BOT_HUB_URL работает слушатель, ноутбук становится «агентом» и забирает свои
апдейты через ретранслятор. Раз в минуту роль перепроверяется и (на проде) идут проверки алертов.
"""
import logging
import time

import requests
from django.core.cache import cache
from django.core.management.base import BaseCommand

from adminbot import alerts, debug, handlers, incidents, relay, router
from adminbot import telegram as tg

logger = logging.getLogger("adminbot")
RECHECK = 60
# Долгий опрос Telegram; заодно шаг проверки запущенных скриптов.
POLL_SECONDS = 8
ALERTS_EVERY = 60
DEPLOYS_EVERY = 30


class Command(BaseCommand):
    help = "Telegram-бот администрирования (слушатель или агент)."

    def add_arguments(self, parser):
        parser.add_argument("--role", choices=["auto", "listener", "agent"], default="auto")

    def handle(self, *args, role="auto", **opts):
        try:
            self.run(role)
        except KeyboardInterrupt:
            # Ctrl+C — обычная остановка, без простыни трейсбека.
            self.stdout.write("\nБот остановлен.")

    def run(self, role: str):
        if not tg.enabled():
            self.stdout.write("Бот выключен: нет ADMIN_BOT_TOKEN или ADMIN_BOT_ENABLED=False. Жду.")
            while True:   # не выходим, чтобы Docker не перезапускал контейнер по кругу
                time.sleep(3600)
        self.fixed = role != "auto"
        self.role = role if self.fixed else self.decide()
        self.stdout.write(f"Бот запущен: {handlers.ENV_LABEL[handlers.ENV]}, роль — {self.role}.")
        last_check = time.monotonic()
        self.last_alerts = 0.0
        self.last_escalation = 0.0
        self.last_deploys = 0.0
        if alerts.enabled():
            self.stdout.write("Алерты включены: проверка раз в минуту.")
        while True:
            try:
                self.listen_once() if self.role == "listener" else self.agent_once()
            except tg.TelegramError as e:
                if e.code == 409:
                    logger.warning("adminbot: бота слушает другой процесс, жду")
                    time.sleep(5)
                else:
                    logger.exception("adminbot: Telegram error")
                    time.sleep(3)
            except requests.RequestException as e:
                logger.warning("adminbot: сеть: %s", tg.redact(e))
                time.sleep(5)
            except Exception:
                logger.exception("adminbot: сбой цикла")
                time.sleep(3)
            try:
                debug.beat(self.role, [p.get("title") for p in alerts.active()])
            except Exception:
                pass
            try:
                handlers.check_runs()
            except Exception:
                logger.exception("adminbot: проверка запущенных скриптов упала")
            if time.monotonic() - self.last_deploys > DEPLOYS_EVERY:
                self.last_deploys = time.monotonic()
                try:
                    handlers.check_deploys()
                except Exception:
                    logger.exception("adminbot: проверка деплоя упала")
            if time.monotonic() - self.last_escalation > ALERTS_EVERY:
                # Дублирует minute_tick Celery: инциденты эскалируются, даже когда Celery стоит.
                self.last_escalation = time.monotonic()
                try:
                    incidents.escalate_due()
                except Exception:
                    logger.exception("adminbot: эскалация инцидентов упала")
            if alerts.enabled() and time.monotonic() - self.last_alerts > ALERTS_EVERY:
                self.last_alerts = time.monotonic()
                try:
                    alerts.run_checks()
                except Exception:
                    logger.exception("adminbot: проверка алертов упала")
            if not self.fixed and time.monotonic() - last_check > RECHECK:
                last_check = time.monotonic()
                new = self.decide()
                if new != self.role:
                    self.stdout.write(f"Роль меняется: {self.role} → {new}.")
                    self.role = new

    def decide(self) -> str:
        if handlers.ENV == "prod":
            return "listener"
        hub = relay.hub_status()
        if hub and hub.get("listener") and hub["listener"] != handlers.ENV:
            return "agent"
        return "listener"

    def listen_once(self):
        relay.mark_listener(handlers.ENV)
        offset = cache.get("adminbot:offset")
        for update in tg.get_updates(offset, timeout=POLL_SECONDS):
            cache.set("adminbot:offset", update["update_id"] + 1, None)
            debug.got_update()
            try:
                router.route(update)
            except Exception:
                logger.exception("adminbot: ошибка обработки апдейта")
        relay.mark_listener(handlers.ENV)

    def agent_once(self):
        for update in relay.fetch(handlers.ENV):
            debug.got_update()
            try:
                handlers.handle(update)
            except Exception:
                logger.exception("adminbot: ошибка обработки апдейта (агент)")
