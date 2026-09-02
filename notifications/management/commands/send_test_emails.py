# notifications/management/commands/send_test_emails.py
"""
Отправляет по одному образцу КАЖДОГО письма DOPX на реальную почту — чтобы
визуально проверить редизайн 2026-09-02 (единый премиальный вид всех писем,
templates/emails/*.html + общий каркас в templates/emails/partials/) вживую
в почтовом клиенте, а не только в браузерном превью render_to_string.

Получатель по умолчанию — email аккаунта с username='admin' (или, если
такого нет, первого активного суперпользователя). Это НАМЕРЕННО не
параметр без значения по умолчанию: продуктовый запрос был буквально
"пришли мне на почту, привязанную к аккаунту admin".

ВАЖНО про фикстуры: НИЧЕГО не сохраняется в БД. Все объекты (Match,
ContactSubmission, RoundBestXI, SuspiciousActivityFlag, Notification)
создаются как несохранённые Python-инстансы моделей — этого достаточно для
рендеринга шаблона (методы вроде get_score_display()/get_category_display()
читают только поля объекта, к БД не обращаются), и не оставляет в базе
мусорных тестовых обращений/матчей после каждого прогона команды. Team
запрашиваются из БД по-настоящему (это статичный справочник клубов лиги,
почти наверняка уже заполнен и не содержит ничьих личных данных) — если
команд в базе меньше двух, используется тот же несохранённый паттерн.

Каждое письмо отправляется ЧЕРЕЗ РЕАЛЬНЫЙ EMAIL_BACKEND (не через
notifications.tasks._send_email_to_user — та функция в консольном/
неполном SMTP-окружении молча притворяется, что письмо отправлено, см. её
докстринг, а команде важно узнать о реальном сбое доставки, а не скрыть
его), с тем же приёмом «HTML + strip_tags() как plain-text alternative»,
что теперь используют все боевые пути отправки в проекте (см. правки
2026-09-02 в notifications/tasks.py, core/views.py, notifications/admin.py,
parsers/tasks.py).
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import strip_tags

TEST_SUBJECT_PREFIX = "[DOPX TEST] "


class Command(BaseCommand):
    help = (
        "Отправляет по одному образцу каждого письма DOPX (редизайн 2026-09-02) "
        "на почту аккаунта admin — для визуальной проверки вживую."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email", type=str, default=None,
            help="Отправить на этот адрес вместо email аккаунта admin.",
        )
        parser.add_argument(
            "--only", type=str, default=None,
            help="Через запятую: отправить только эти письма по ключу (см. список в коде).",
        )

    def handle(self, *args, **options):
        User = self._user_model()
        recipient_email = options.get("email")
        admin_user = User.objects.filter(username="admin").first()
        if not admin_user:
            admin_user = User.objects.filter(is_superuser=True, is_active=True).order_by("date_joined").first()

        if not recipient_email:
            if not admin_user or not admin_user.email:
                raise CommandError(
                    "Не найден пользователь 'admin' с заполненным email (и нет ни одного "
                    "активного суперпользователя с email). Укажите адрес явно: --email you@example.com"
                )
            recipient_email = admin_user.email

        site_url = getattr(settings, "SITE_URL", "https://dopx.kz")
        site_name = "DOPX"

        # admin_user используется как "user" контекста в письмах, где он
        # нужен — если реального admin-аккаунта нет (только --email), делаем
        # лёгкий несохранённый User с тем именем, что и в письме, чтобы
        # шаблоны с {{ user.username }} не падали на None.
        preview_user = admin_user or User(username="admin", email=recipient_email)

        messages = self._build_messages(preview_user, site_url, site_name)

        only = options.get("only")
        if only:
            wanted = {key.strip() for key in only.split(",") if key.strip()}
            unknown = wanted - {m["key"] for m in messages}
            if unknown:
                raise CommandError(f"Неизвестные ключи в --only: {', '.join(sorted(unknown))}")
            messages = [m for m in messages if m["key"] in wanted]

        self.stdout.write(f"Отправляю {len(messages)} писем на {recipient_email}...\n")

        sent, failed = 0, []
        for item in messages:
            try:
                html_message = render_to_string(item["template"], item["context"])
                email = EmailMultiAlternatives(
                    subject=TEST_SUBJECT_PREFIX + item["subject"],
                    body=strip_tags(html_message),
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@dopx.kz"),
                    to=[recipient_email],
                )
                email.attach_alternative(html_message, "text/html")
                email.send(fail_silently=False)
                self.stdout.write(self.style.SUCCESS(f"  ✅ {item['key']} — {item['subject']}"))
                sent += 1
            except Exception as exc:  # noqa: BLE001 — сбой одного письма не должен прервать остальные
                self.stdout.write(self.style.ERROR(f"  ❌ {item['key']}: {type(exc).__name__}: {exc}"))
                failed.append(item["key"])

        self.stdout.write("")
        if failed:
            self.stdout.write(self.style.WARNING(f"Отправлено {sent}/{len(messages)}. Не удалось: {', '.join(failed)}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"Готово — все {sent} писем отправлены на {recipient_email}."))

    @staticmethod
    def _user_model():
        from django.contrib.auth import get_user_model
        return get_user_model()

    def _build_messages(self, user, site_url: str, site_name: str) -> list[dict]:
        team_a, team_b, team_c, team_d = self._sample_teams()
        now = timezone.now()

        finished_match = self._unsaved_match(team_a, team_b, home_score=2, away_score=1, status="finished", start_time=now - timedelta(hours=2))
        dramatic_match = self._unsaved_match(team_c, team_d, home_score=3, away_score=3, status="finished", start_time=now - timedelta(days=2))
        upcoming_match = self._unsaved_match(team_a, team_c, home_score=None, away_score=None, status="scheduled", start_time=now + timedelta(hours=1))

        submission = self._unsaved_contact_submission(user)
        ticket = self._unsaved_contact_submission(user, status="resolved")
        round_xi = self._unsaved_round_xi(team_b, dramatic_match)
        antifraud_flag = self._unsaved_suspicious_flag(user)
        digest_notifications = self._sample_notifications(user)

        prediction_counts = {"total": 128, "home_pct": 54, "draw_pct": 21, "away_pct": 25}

        return [
            {
                "key": "welcome", "template": "emails/welcome.html",
                "subject": "Добро пожаловать в DOPX",
                "context": {"user": user, "site_url": site_url, "site_name": site_name},
            },
            {
                "key": "verify_email", "template": "emails/verify_email.html",
                "subject": "Подтвердите email на DOPX",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "verify_url": f"{site_url}/users/verify-email/preview-token/"},
            },
            {
                "key": "password_reset", "template": "emails/password_reset_email.html",
                "subject": "Сброс пароля DOPX",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "protocol": "https", "domain": site_url.split("://")[-1],
                             "uid": "MQ", "token": "preview-token-not-valid"},
            },
            {
                "key": "notification_digest", "template": "emails/notification_digest.html",
                "subject": f"Ваши обновления на DOPX ({len(digest_notifications)})",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "notifications": digest_notifications, "count": len(digest_notifications)},
            },
            {
                "key": "badge_earned", "template": "emails/badge_earned.html",
                "subject": "Новое достижение: Полиглот лиги",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "badge_name": "Полиглот лиги"},
            },
            {
                "key": "level_up", "template": "emails/level_up.html",
                "subject": "Вы достигли уровня 12!",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "new_level": 12, "total_xp": 4820},
            },
            {
                "key": "weekly_summary", "template": "emails/weekly_summary.html",
                "subject": "Ваша неделя на DOPX",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "evaluations_count": 14, "predictions_count": 6, "accuracy_pct": 67,
                             "top_match": finished_match},
            },
            {
                "key": "voting_open", "template": "emails/voting_open.html",
                "subject": f"{team_a.name} {finished_match.get_score_display()} {team_b.name} — голосование открыто",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "match": finished_match, "title": f"{team_a.name} {finished_match.get_score_display()} {team_b.name}"},
            },
            {
                "key": "voting_closing", "template": "emails/voting_closing.html",
                "subject": f"Голосование за матч {team_a.name} vs {team_b.name} скоро закроется",
                "context": {"user": user, "site_url": site_url, "site_name": site_name, "match": finished_match},
            },
            {
                "key": "prediction_closing", "template": "emails/prediction_closing.html",
                "subject": f"{team_a.name} — {team_c.name}: как думаете, кто победит?",
                "context": {"user": user, "site_url": site_url, "site_name": site_name, "match": upcoming_match},
            },
            {
                "key": "prediction_result_correct", "template": "emails/prediction_result.html",
                "subject": f"Прогноз сбылся: {team_a.name} vs {team_b.name}",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "match": finished_match, "counts": prediction_counts,
                             "is_correct": True, "your_choice_label": team_a.name},
            },
            {
                "key": "prediction_result_wrong", "template": "emails/prediction_result.html",
                "subject": f"Итог матча: {team_a.name} vs {team_b.name}",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "match": finished_match, "counts": prediction_counts,
                             "is_correct": False, "your_choice_label": "Ничья"},
            },
            {
                "key": "round_results", "template": "emails/round_results.html",
                "subject": f"{round_xi.brand_title} готовы",
                "context": {"user": user, "site_url": site_url, "site_name": site_name, "round_xi": round_xi},
            },
            {
                "key": "system_announcement", "template": "emails/system_announcement.html",
                "subject": "Обновление платформы | DOPX",
                "context": {"user": user, "site_url": site_url, "site_name": site_name,
                             "title": "Новый сезон КПЛ уже на DOPX",
                             "body": "Мы обновили таблицы под новый сезон и добавили живые трансляции счёта.\n\nСпасибо, что остаётесь с нами."},
            },
            {
                "key": "contact_confirmation", "template": "emails/contact_confirmation.html",
                "subject": f"Ваше обращение #{str(submission.id)[:8]} принято",
                "context": {"submission": submission, "username": user.username,
                             "site_name": site_name, "site_url": site_url},
            },
            {
                "key": "ticket_status_change", "template": "emails/ticket_status_change.html",
                "subject": f"Статус обращения #{str(ticket.id)[:8]} изменён | DOPX",
                "context": {"ticket": ticket, "old_status": "В работе", "new_status": "Решено",
                             "site_name": site_name, "site_url": site_url},
            },
            {
                "key": "contact_form", "template": "emails/contact_form.html",
                "subject": f"Новое обращение #{str(submission.id)[:8]} ({submission.get_category_display()})",
                "context": {"submission": submission, "category": submission.get_category_display(),
                             "email": user.email, "username": user.username,
                             "has_attachment": False, "site_name": site_name, "site_url": site_url},
            },
            {
                "key": "staff_antifraud_digest", "template": "emails/staff_antifraud_digest.html",
                "subject": "Антифрод за неделю: 3 новых сигнала",
                "context": {"site_url": site_url, "site_name": site_name,
                             "total_new": 3, "open_disputes": 1,
                             "by_source": {"Слишком быстрое заполнение вайзарда оценки": 2, "Аномальный всплеск голосования": 1},
                             "top_flags": [antifraud_flag]},
            },
            {
                "key": "sync_error_alert", "template": "emails/sync_error_alert.html",
                "subject": "DOPX Sync Alert [sync_monitoring]",
                "context": {"site_url": site_url, "site_name": site_name,
                             "alert_type": "sync_monitoring",
                             "error_message": "Проблемы с синхронизацией:\n- 3 матча без составов спустя 30+ мин после начала\n- 1 матч без событий спустя 45+ мин",
                             "extra_data": {"matches_without_lineups": 3, "matches_without_events": 1},
                             "timestamp": now},
            },
        ]

    def _sample_teams(self):
        from teams.models import Team

        real_teams = list(Team.objects.order_by("name")[:4])
        placeholders = ["Кайрат", "Астана", "Тобол", "Ордабасы"]
        while len(real_teams) < 4:
            real_teams.append(Team(name=placeholders[len(real_teams)]))
        return real_teams[0], real_teams[1], real_teams[2], real_teams[3]

    @staticmethod
    def _unsaved_match(home_team, away_team, *, home_score, away_score, status, start_time):
        from matches.models import Match

        return Match(
            home_team=home_team, away_team=away_team,
            home_score=home_score, away_score=away_score,
            status=status, start_time=start_time,
        )

    @staticmethod
    def _unsaved_contact_submission(user, status: str = "new"):
        from notifications.models import ContactSubmission

        return ContactSubmission(
            user=user,
            category="bug",
            subject="Не открывается страница матча",
            message=(
                "Здравствуйте! При открытии страницы матча выдаёт ошибку 500. "
                "Пробовал с телефона и с компьютера — одинаково."
            ),
            status=status,
            created_at=timezone.now(),
        )

    @staticmethod
    def _unsaved_round_xi(team, dramatic_match):
        from round_squad.models import RoundBestXI

        return RoundBestXI(
            season_id=uuid.uuid4(),
            tour=7,
            player_of_round_name="Данияр Тлеубаев",
            player_of_round_team_name=team.name,
            player_of_round_score=8.7,
            most_dramatic_match=dramatic_match,
            created_at=timezone.now(),
        )

    @staticmethod
    def _unsaved_suspicious_flag(user):
        from users.models import SuspiciousActivityFlag

        return SuspiciousActivityFlag(
            user=user,
            source="fast_wizard",
            score=0.82,
            details={"duration_seconds": 4.2, "threshold_seconds": 15},
            created_at=timezone.now(),
        )

    @staticmethod
    def _sample_notifications(user):
        from notifications.models import Notification

        now = timezone.now()
        return [
            Notification(
                user=user, notification_type="new_badge",
                title="Новое достижение: Полиглот лиги",
                message='Новый значок в вашей коллекции DOPX.',
                created_at=now - timedelta(hours=3),
            ),
            Notification(
                user=user, notification_type="voting_open",
                title="Матч завершён — голосование открыто",
                message="Кайрат 2:1 Астана — поделитесь мнением об игре.",
                created_at=now - timedelta(hours=6),
            ),
        ]
