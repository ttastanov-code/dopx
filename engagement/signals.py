# engagement/signals.py
"""Прогресс заданий дня и награды за приглашения по событиям моделей."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from evaluations.models import EvaluationSession
from matches.models import MatchReaction
from predictions.models import MatchPrediction
from users.models import Follow, UserXP

from .models import DailyQuest, DailyStreak, SeasonPass


def _bump_progress(sender, instance, **kwargs):
    from core.live import bump_user_version

    user_id = instance.user_id
    transaction.on_commit(lambda: bump_user_version(user_id))


# Личный прогресс изменился — открытые вкладки пользователя обновятся по пингу версии.
for _model in (DailyStreak, DailyQuest, SeasonPass, UserXP):
    post_save.connect(_bump_progress, sender=_model, dispatch_uid=f"live-progress-{_model.__name__}")


@receiver(post_save, sender=MatchPrediction)
def on_prediction(sender, instance, created, **kwargs):
    if created:
        from engagement.quests import track

        transaction.on_commit(lambda: track(instance.user, "predict"))


@receiver(post_save, sender=MatchReaction)
def on_reaction(sender, instance, created, **kwargs):
    if created:
        from engagement.quests import track

        transaction.on_commit(lambda: track(instance.user, "react"))


@receiver(post_save, sender=EvaluationSession)
def on_session(sender, instance, created, update_fields=None, **kwargs):
    if instance.status != "completed":
        return
    from engagement.quests import track
    from engagement.referrals import reward_if_due

    user = instance.user

    def _after():
        track(user, "evaluate")
        reward_if_due(user)

    transaction.on_commit(_after)


@receiver(post_save, sender=Follow)
def on_follow(sender, instance, created, **kwargs):
    if created and instance.player_id:
        from engagement.quests import track

        transaction.on_commit(lambda: track(instance.user, "follow_player"))
