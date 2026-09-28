# engagement/signals.py
"""Прогресс заданий дня и награды за приглашения по событиям моделей."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from evaluations.models import EvaluationSession
from matches.models import MatchReaction
from predictions.models import MatchPrediction
from users.models import Follow


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
