# users/emails.py
"""Почта аккаунта: занятость адреса, смена только через подтверждение, полнота профиля."""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.utils import canonical_email

CONFIRM_TTL = timedelta(hours=48)


def email_taken(email: str, exclude_user=None) -> bool:
    """Адрес у другого аккаунта сейчас или был у него подтверждён раньше («+метки» и точки Gmail — тот же адрес)."""
    from .models import UsedEmail, User

    canon = canonical_email(email)
    users = User.objects.filter(Q(email__iexact=email) | Q(email_canonical=canon))
    used = UsedEmail.objects.filter(email_canonical=canon)
    if exclude_user is not None:
        users, used = users.exclude(pk=exclude_user.pk), used.exclude(user_id=exclude_user.pk)
    return users.exists() or used.exists()


def mark_confirmed(user) -> None:
    """Текущая почта подтверждена: отметка времени и закрепление адреса за аккаунтом."""
    from .models import UsedEmail, User

    now = timezone.now()
    User.objects.filter(pk=user.pk).update(email_verified_at=now)
    user.email_verified_at = now
    UsedEmail.objects.get_or_create(user_id=user.pk, email_canonical=canonical_email(user.email))


def request_change(user, new_email: str) -> None:
    """Новый адрес ждёт подтверждения; текущий продолжает работать (сменой почты нельзя заблокировать себе вход)."""
    from .models import User

    from notifications.tasks import send_email_change_confirmation

    User.objects.filter(pk=user.pk).update(pending_email=new_email)
    user.pending_email = new_email
    user.refresh_verification_token()
    token = str(user.verification_token)
    transaction.on_commit(lambda: send_email_change_confirmation.delay(str(user.pk), token))


def confirm_change(token) -> tuple[object | None, str]:
    """Переход по ссылке из письма: pending_email становится почтой. (пользователь, ошибка)."""
    from .models import User

    user = User.objects.filter(verification_token=token).exclude(pending_email="").first()
    if user is None:
        return None, "Ссылка недействительна или уже использована."
    if timezone.now() - user.verification_token_created_at > CONFIRM_TTL:
        return None, "Ссылка устарела. Укажите почту ещё раз, и мы пришлём новую."
    new_email = user.pending_email
    if email_taken(new_email, exclude_user=user):
        User.objects.filter(pk=user.pk).update(pending_email="")
        return None, "Эта почта уже привязана к другому аккаунту."
    with transaction.atomic():
        user.email, user.pending_email = new_email, ""
        user.save(update_fields=["email", "pending_email", "updated_at"])
        user.refresh_verification_token()  # ссылка одноразовая
        mark_confirmed(user)
    return user, ""


def profile_complete(user) -> bool:
    """Город и подтверждённая настоящая почта — без этого оценки и прогнозы закрыты (PROFILE_REQUIRED)."""
    return bool(user.city) and user.has_real_email and user.email_verified_at is not None


def missing_fields(user) -> list[str]:
    out = []
    if not user.city:
        out.append("город")
    if not user.has_real_email:
        out.append("почта")
    elif user.email_verified_at is None:
        out.append("подтверждение почты")
    return out
