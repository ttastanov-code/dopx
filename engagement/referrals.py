# engagement/referrals.py
"""Приглашения: /r/<code>/ запоминает пригласившего в сессии, регистрация связывает,
награда обоим — когда приглашённый завершит первую оценку матча."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

SESSION_KEY = "referral_code"
SESSION_SOURCE = "referral_source"
REWARD_XP = 100
# Сколько приглашённых -> достижение пригласившего.
RECRUITER_BADGES = {1: "recruiter_1", 5: "recruiter_5", 20: "recruiter_20"}


def code_for(user) -> str:
    from engagement.models import ReferralCode

    return ReferralCode.objects.get_or_create(user=user)[0].code


def remember(request, code: str, source: str) -> None:
    request.session[SESSION_KEY] = code
    request.session[SESSION_SOURCE] = source


def attach(request, user) -> None:
    """После регистрации: связать с пригласившим (не себя и не повторно)."""
    from engagement.models import Referral, ReferralCode

    code = request.session.pop(SESSION_KEY, None)
    source = request.session.pop(SESSION_SOURCE, Referral.SOURCE_LINK)
    if not code:
        return
    ref = ReferralCode.objects.filter(code=code).select_related("user").first()
    if ref is None or ref.user_id == user.pk or Referral.objects.filter(invited=user).exists():
        return
    Referral.objects.create(inviter=ref.user, invited=user, source=source)
    from engagement.quests import record

    record(ref.user, "friend_registered", user.pk)


def reward_if_due(user) -> None:
    """Первая завершённая оценка приглашённого — XP обоим и достижения."""
    from engagement.models import Referral
    from engagement.rewards import award_badge, award_xp

    with transaction.atomic():
        referral = Referral.objects.select_for_update().filter(invited=user, rewarded_at__isnull=True).select_related("inviter").first()
        if referral is None:
            return
        referral.rewarded_at = timezone.now()
        referral.save(update_fields=["rewarded_at", "updated_at"])
    award_xp(referral.inviter, REWARD_XP, "приглашённый друг оценил матч")
    award_xp(user, REWARD_XP, "первая оценка по приглашению")
    award_badge(user, "came_with_friend")
    from engagement.notify import referral_rewarded
    referral_rewarded(referral.inviter, user, REWARD_XP)
    rewarded = Referral.objects.filter(inviter=referral.inviter, rewarded_at__isnull=False).count()
    for threshold, badge in RECRUITER_BADGES.items():
        if rewarded >= threshold:
            award_badge(referral.inviter, badge)


def stats(user) -> dict:
    from engagement.models import Referral

    sent = Referral.objects.filter(inviter=user)
    return {"joined": sent.count(), "active": sent.filter(rewarded_at__isnull=False).count()}
