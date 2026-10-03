# users/reports.py
"""Жалобы болельщиков: создание с лимитом и меры модерации. Общее для сайта, дашборда и бота."""
from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import User, UserReport

DAILY_LIMIT = 10

# действие -> (подпись, для кого: user/league, нужное право)
ACTIONS = {
    "reject": ("Отклонить, нарушений нет", None, None),
    "clear_avatar": ("Убрать аватар", "user", "users.change_user"),
    "clear_bio": ("Очистить «О себе»", "user", "users.change_user"),
    "reset_username": ("Сбросить ник", "user", "users.change_user"),
    "ban": ("Заблокировать аккаунт", "user", "users.change_user"),
    "rename_league": ("Сбросить название лиги", "league", "engagement.change_friendleague"),
    "delete_league": ("Удалить лигу", "league", "engagement.delete_friendleague"),
}


class ReportError(Exception):
    pass


def create(reporter, *, reason: str, comment: str = "", target_user=None, league=None) -> UserReport:
    """Новая жалоба; повтор на ту же цель не плодим, больше DAILY_LIMIT в сутки — отказ."""
    if reason not in dict(UserReport.REASON_CHOICES):
        raise ReportError("Выберите причину жалобы.")
    if target_user is not None and target_user.pk == reporter.pk:
        raise ReportError("На себя пожаловаться нельзя.")
    open_qs = UserReport.objects.filter(reporter=reporter, status="new")
    existing = open_qs.filter(target_user=target_user, friend_league=league).first()
    if existing:
        return existing
    since = timezone.now() - timedelta(days=1)
    if UserReport.objects.filter(reporter=reporter, created_at__gte=since).count() >= DAILY_LIMIT:
        raise ReportError("Слишком много жалоб за сутки. Попробуйте завтра.")
    return UserReport.objects.create(
        reporter=reporter, target_user=target_user, friend_league=league, reason=reason,
        comment=(comment or "").strip()[:300],
        target_name=(target_user.username if target_user else league.name)[:150],
    )


def allowed_actions(report: UserReport, staff) -> list[tuple[str, str]]:
    """Какие кнопки показать этому сотруднику для этой жалобы."""
    kind = "user" if report.target_user_id else ("league" if report.friend_league_id else None)
    protected = bool(report.target_user_id and (report.target_user.is_staff or report.target_user.is_superuser))
    out = []
    for key, (label, target, perm) in ACTIONS.items():
        if target and (target != kind or protected):
            continue
        if perm and not (staff.is_superuser or staff.has_perm(perm)):
            continue
        if key == "clear_avatar" and not report.target_user.avatar:
            continue
        if key == "clear_bio" and not report.target_user.bio:
            continue
        if key == "ban" and not report.target_user.is_active:
            continue
        out.append((key, label))
    return out


def _notify(user, text: str) -> None:
    from notifications.models import Notification

    # email_sent_at — служебная заметка только в колокольчике, без письма.
    Notification.objects.create(user=user, notification_type="system", title="Модерация DOPX", message=text,
                                action_url="/users/profile/", email_sent_at=timezone.now())


def apply(report: UserReport, action: str, staff) -> str:
    """Применить меру, закрыть эту и все открытые жалобы на ту же цель. Возвращает текст для сотрудника."""
    if report.status != "new":
        raise ReportError("Жалоба уже разобрана.")
    if action not in dict(allowed_actions(report, staff)):
        raise ReportError("Это действие недоступно.")
    target, league = report.target_user, report.friend_league
    with transaction.atomic():
        if action == "clear_avatar":
            target.avatar.delete(save=False)
            target.avatar = None
            target.save(update_fields=["avatar"])
            _notify(target, "Ваш аватар убран модератором: он нарушал правила сообщества.")
            message = f"Аватар {target.username} убран."
        elif action == "clear_bio":
            target.bio = ""
            target.save(update_fields=["bio"])
            _notify(target, "Описание «О себе» очищено модератором: оно нарушало правила сообщества.")
            message = f"Описание {target.username} очищено."
        elif action == "reset_username":
            old = target.username
            new = _free_username()
            target.username = new
            target.save(update_fields=["username"])
            _notify(target, f"Ник «{old}» нарушал правила сообщества и заменён на «{new}». Новый ник можно выбрать в профиле.")
            message = f"Ник {old} сброшен на {new}."
        elif action == "ban":
            target.is_active = False
            target.save(update_fields=["is_active"])
            from aggregates.tasks import recalculate_matches_for_user
            user_id = str(target.pk)
            transaction.on_commit(lambda: recalculate_matches_for_user.delay(user_id))
            message = f"{target.username} заблокирован."
        elif action == "rename_league":
            old = league.name
            league.name = "Лига друзей"
            league.save(update_fields=["name"])
            _notify(league.owner, f"Название лиги «{old}» нарушало правила сообщества и сброшено. Можно задать новое.")
            message = f"Название лиги «{old}» сброшено."
        elif action == "delete_league":
            _notify(league.owner, f"Лига «{league.name}» удалена модератором за нарушение правил сообщества.")
            message = f"Лига «{league.name}» удалена."
        else:
            message = "Жалоба отклонена."
        same = UserReport.objects.filter(status="new")
        if target:
            same = same.filter(target_user=target)
        elif league:
            same = same.filter(friend_league=league)
        else:
            same = same.filter(pk=report.pk)
        same.update(status="rejected" if action == "reject" else "resolved", action=action,
                    handled_by=staff, handled_at=timezone.now())
        if action == "delete_league":
            league.delete()
    return message


def _free_username() -> str:
    while True:
        name = f"fan{uuid.uuid4().hex[:8]}"
        if not User.objects.filter(username=name).exists():
            return name
