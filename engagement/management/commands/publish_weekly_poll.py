# engagement/management/commands/publish_weekly_poll.py
"""manage.py publish_weekly_poll --kind episode|duel [--apply] [--no-push]

Опрос недели вне расписания. Без --apply — показывает, что будет выбрано (кандидата и индекс).
--no-push — опубликовать без push активным пользователям.
"""
from django.core.management.base import BaseCommand

from engagement import polls


class Command(BaseCommand):
    help = "Опубликовать «Спорный момент» или «Дуэль тура» сейчас."

    def add_arguments(self, parser):
        parser.add_argument("--kind", choices=["episode", "duel"], default="episode")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--no-push", action="store_true")

    def handle(self, *args, kind, apply, no_push, **options):
        matches = polls.last_round_matches()
        tour = matches[0].tour if matches else None
        self.stdout.write(f"Матчей последнего тура: {len(matches)} (тур {tour or '—'})")
        if kind == "episode":
            found = polls.pick_episode(matches)
            if not found:
                self.stdout.write(self.style.WARNING("Спорных эпизодов не найдено (или все уже были в опросах)."))
                return
            event, match, texts, score = found
            self.stdout.write(f"Эпизод: {match} {event.minute}' {event.get_event_type_display()} — индекс {score}")
            self.stdout.write(f"Вопрос: {texts[0]} [{texts[1]} / {texts[2]}] — {texts[3]}")
        else:
            pair = polls.pick_duel(matches)
            if not pair:
                self.stdout.write(self.style.WARNING("Нет пары для дуэли: мало оценок и статистики тура."))
                return
            for p in pair:
                self.stdout.write(f"  {p[0].full_name} ({p[1]}) — {p[3]}")
        if not apply:
            self.stdout.write(self.style.NOTICE("Dry-run: добавьте --apply, чтобы опубликовать."))
            return
        poll = polls.create_poll(kind, force=True)
        if poll is None:
            self.stdout.write(self.style.ERROR("Не удалось создать опрос."))
            return
        sent = 0
        if not no_push:
            from engagement.notify import poll_published
            sent = poll_published(poll)
        self.stdout.write(self.style.SUCCESS(f"Опубликовано: {poll.question}. Push получателей: {sent}."))
