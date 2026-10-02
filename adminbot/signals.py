# adminbot/signals.py
"""События, о которых бот пишет сотрудникам."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from engagement.models import ExpertTake
from notifications.models import ContactSubmission
from users.models import SuspiciousActivityFlag

from .tasks import FLAG_NOTIFY_SCORE


@receiver(post_save, sender=ExpertTake, dispatch_uid="adminbot-expert-take")
def on_expert_take(sender, instance, created, **kwargs):
    # Только присланные экспертом по ссылке и ждущие проверки.
    if created and instance.invite_id and not instance.is_published:
        from .notify import expert_take
        expert_take(instance)


@receiver(post_save, sender=SuspiciousActivityFlag, dispatch_uid="adminbot-flag")
def on_flag(sender, instance, created, **kwargs):
    if created and instance.status == "pending" and instance.score >= FLAG_NOTIFY_SCORE:
        from .notify import antifraud_flag
        antifraud_flag(instance)


@receiver(post_save, sender=ContactSubmission, dispatch_uid="adminbot-contact")
def on_contact(sender, instance, created, **kwargs):
    if created:
        from .notify import contact
        contact(instance)
