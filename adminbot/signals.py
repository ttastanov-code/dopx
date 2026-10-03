# adminbot/signals.py
"""События, о которых бот пишет сотрудникам и в канал."""
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from engagement.models import ExpertTake
from matches.models import Match
from notifications.models import ContactSubmission
from parsers.models import ParserDiscrepancy
from round_squad.models import RoundBestXI
from users.models import SuspiciousActivityFlag, UserReport

from .tasks import FLAG_NOTIFY_SCORE

MATCH_FIELDS = ("status", "home_score", "away_score")


def _safe(fn, *args):
    # Уведомление не должно ломать сохранение данных.
    import logging

    try:
        fn(*args)
    except Exception:
        logging.getLogger(__name__).exception("adminbot: сигнал %s упал", getattr(fn, "__name__", fn))


@receiver(post_save, sender=ExpertTake, dispatch_uid="adminbot-expert-take")
def on_expert_take(sender, instance, created, **kwargs):
    # Только присланные экспертом по ссылке и ждущие проверки.
    if created and instance.invite_id and not instance.is_published:
        from .notify import expert_take
        _safe(expert_take, instance)
    if instance.is_published and not getattr(instance, "_was_published", False):
        from .channel import expert_post
        _safe(expert_post, instance)


@receiver(pre_save, sender=ExpertTake, dispatch_uid="adminbot-expert-take-before")
def before_expert_take(sender, instance, **kwargs):
    instance._was_published = bool(instance.pk) and ExpertTake.objects.filter(pk=instance.pk, is_published=True).exists()


@receiver(post_save, sender=SuspiciousActivityFlag, dispatch_uid="adminbot-flag")
def on_flag(sender, instance, created, **kwargs):
    if created and instance.status == "pending" and instance.score >= FLAG_NOTIFY_SCORE:
        from .notify import antifraud_flag
        _safe(antifraud_flag, instance)


@receiver(post_save, sender=UserReport, dispatch_uid="adminbot-user-report")
def on_user_report(sender, instance, created, **kwargs):
    if created:
        from .notify import user_report
        _safe(user_report, instance)


@receiver(post_save, sender=ContactSubmission, dispatch_uid="adminbot-contact")
def on_contact(sender, instance, created, **kwargs):
    if created:
        from .notify import contact
        _safe(contact, instance)


@receiver(post_save, sender=ParserDiscrepancy, dispatch_uid="adminbot-discrepancy")
def on_discrepancy(sender, instance, created, **kwargs):
    if created:
        from .notify import discrepancy
        _safe(discrepancy, instance)


@receiver(pre_save, sender=Match, dispatch_uid="adminbot-match-before")
def before_match(sender, instance, **kwargs):
    instance._bot_before = (dict(Match.objects.filter(pk=instance.pk).values(*MATCH_FIELDS).first() or {})
                            if instance.pk else {})


@receiver(post_save, sender=Match, dispatch_uid="adminbot-match")
def on_match(sender, instance, created, **kwargs):
    before = getattr(instance, "_bot_before", {})
    if created or not before or all(before.get(f) == getattr(instance, f) for f in MATCH_FIELDS):
        return
    from .matchday import on_match_change
    _safe(on_match_change, instance, before)


@receiver(post_save, sender=RoundBestXI, dispatch_uid="adminbot-round")
def on_round(sender, instance, **kwargs):
    if instance.is_final:
        from .matchday import on_round_final
        _safe(on_round_final, instance)
