# notifications/management/commands/send_test_push.py
"""manage.py send_test_push <username> [--kind match_event | --all-kinds]

Отправляет тестовый push на все устройства пользователя и показывает результат по каждому.
Текст — пример реального уведомления этого типа. Настройки пользователя и режим траура
учитываются, как в бою: выключенный тип не придёт.
"""
import time

from django.core.management.base import BaseCommand, CommandError

from notifications.services import PUSH_PROFILES, send_push_to_users
from users.models import PushSubscription, User

# Примеры текстов по типам: (заголовок, текст, ссылка).
SAMPLES = {
    "match_event": ("⚽ ГОЛ! Кайрат 1:0 Астана", "34', Садыбеков.", "/matches/"),
    "match_started": ("🟢 Матч начался", "Кайрат — Астана: следите за событиями вживую.", "/matches/"),
    "lineups_available": ("📋 Составы объявлены", "Кайрат — Астана: стартовые составы уже на сайте.", "/matches/"),
    "prediction_closing": ("⏳ Прогнозы закрываются", "Через час старт Кайрат — Астана. Успейте сделать прогноз.", "/matches/"),
    "voting_open": ("⭐ Оцените матч", "Кайрат 2:1 Астана — голосование открыто.", "/matches/"),
    "match_finished": ("🏁 Финальный свисток", "Кайрат 2:1 Астана. Оцените игроков, пока идёт голосование.", "/matches/"),
    "evaluation_reminder": ("✍️ Допишите оценку", "Голосование по Кайрат — Астана закроется через 2 часа.", "/matches/"),
    "prediction_result": ("🎯 Прогноз сбылся!", "Кайрат 2:1 Астана — вы угадали вместе с 41% болельщиков.", "/matches/"),
    "achievement": ("🎖️ Новое достижение!", "Вы получили достижение: Неделя без пропусков.", "/users/profile/"),
    "round_results": ("🏆 DOPX Лучшие тура", "Ваш игрок попал в сборную 27-го тура.", "/round/"),
    "match_changed": ("📅 Матч перенесён", "Кайрат — Астана: новое время 19:00.", "/matches/"),
    "voting_closing": ("⏰ Голосование закрывается", "Остался час, чтобы оценить Кайрат — Астана.", "/matches/"),
    "ratings_published": ("📊 Рейтинги открыты", "Кайрат — Астана: игрок матча и ваша оценка рядом с общей.", "/matches/"),
    "streak": ("🔥 Не потеряйте серию", "Серия 12 дн. сгорит в полночь. Зайдите на сайт, чтобы её сохранить.", "/"),
    "social": ("👥 Новый участник лиги", "Друг теперь в лиге «Сектор B».", "/friends/"),
    "daily_poll": ("⚖️ Был ли пенальти?", "Астана 1:1 Кайрат, 78'. Рассудите в один тап.", "/#polls"),
    "default": ("DOPX: тестовое уведомление", "Проверка доставки push.", "/"),
}


class Command(BaseCommand):
    help = "Тестовый push на все устройства пользователя."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--kind", default="match_event", choices=sorted(PUSH_PROFILES))
        parser.add_argument("--all-kinds", action="store_true", help="По одному push каждого типа.")

    def handle(self, *args, username, kind, all_kinds, **options):
        user = User.objects.filter(username=username).first()
        if not user:
            raise CommandError(f"Пользователь {username} не найден")

        subs = list(PushSubscription.objects.filter(user=user))
        if not subs:
            self.stdout.write(self.style.WARNING("У пользователя нет push-подписок — включите уведомления в настройках."))
            return
        for s in subs:
            self.stdout.write(f"  устройство: {s.user_agent or '—'}  endpoint={s.endpoint[:60]}…")

        kinds = sorted(PUSH_PROFILES) if all_kinds else [kind]
        total = 0
        for k in kinds:
            title, body, url = SAMPLES.get(k, SAMPLES["default"])
            # Разные tag — иначе устройство покажет только последнее.
            sent = send_push_to_users([user.id], title=title, body=body, url=url, kind=k, tag=f"test-{k}")
            profile = PUSH_PROFILES[k]
            mark = self.style.SUCCESS("✓") if sent else self.style.ERROR("✗")
            self.stdout.write(f"{mark} {k:20} TTL {profile['ttl']} с, {profile['urgency']:6} — {title}")
            total += sent
            if all_kinds:
                time.sleep(1)
        left = PushSubscription.objects.filter(user=user).count()
        style = self.style.SUCCESS if total else self.style.ERROR
        self.stdout.write(style(f"Готово: доставлено {total}, удалено мёртвых подписок: {len(subs) - left}."))
        if not total:
            self.stdout.write("Не пришло: проверьте VAPID-ключи в .env, тумблеры типа в настройках уведомлений, режим траура.")
