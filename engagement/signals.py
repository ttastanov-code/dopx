# engagement/signals.py
"""Прогресс заданий дня (engagement/quests.py) и награды за приглашения по событиям моделей; отмена действий снимает кредит."""
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from django.utils import timezone

from evaluations.models import EvaluationSession
from events.models import EventReaction
from fanbot.models import TelegramAccount
from matches.models import MatchReaction
from predictions.models import MatchPrediction
from users.models import Follow, PushSubscription, UserXP

from .models import DailyQuest, DailyStreak, FriendLeague, FriendLeagueMember, SeasonPass


def _bump_progress(sender, instance, **kwargs):
    from core.live import bump_user_version

    user_id = instance.user_id
    transaction.on_commit(lambda: bump_user_version(user_id))


# Личный прогресс изменился — открытые вкладки пользователя обновятся по пингу версии.
for _model in (DailyStreak, DailyQuest, SeasonPass, UserXP):
    post_save.connect(_bump_progress, sender=_model, dispatch_uid=f"live-progress-{_model.__name__}")


def _record(user, event, ref, **ctx):
    from engagement.quests import record

    transaction.on_commit(lambda: record(user, event, ref, **ctx))


def _revoke(user, event, ref):
    from engagement.quests import revoke

    transaction.on_commit(lambda: revoke(user, event, ref))


@receiver(post_save, sender=MatchPrediction)
def on_prediction(sender, instance, created, **kwargs):
    if created:  # смена исхода — не новый прогноз
        _record(instance.user, "prediction", instance.match_id, tour=instance.match.tour)


@receiver(post_save, sender=MatchReaction)
def on_reaction(sender, instance, created, **kwargs):
    if created:
        _record(instance.user, "match_reaction", instance.match_id)


@receiver(post_save, sender=EventReaction)
def on_event_reaction(sender, instance, created, **kwargs):
    if created:
        _record(instance.user, "event_reaction", instance.match_event_id)


@receiver(post_save, sender=EvaluationSession)
def on_session(sender, instance, created, update_fields=None, **kwargs):
    if instance.status != "completed":
        return
    from datetime import timedelta

    from engagement.referrals import reward_if_due

    user, match = instance.user, instance.match
    done_at = instance.completed_at or timezone.now()
    fresh = bool(match.end_time) and done_at - match.end_time <= timedelta(hours=3)
    _record(user, "evaluation", instance.match_id, fresh=fresh, mode=instance.mode)
    transaction.on_commit(lambda: reward_if_due(user))


@receiver(post_save, sender=Follow)
def on_follow(sender, instance, created, **kwargs):
    if not created:
        return
    if instance.player_id:
        _record(instance.user, "follow_player", instance.player_id)
    if instance.team_id:
        _record(instance.user, "follow_team", instance.team_id)


@receiver(post_delete, sender=Follow)
def on_unfollow(sender, instance, **kwargs):
    """Отписался — подписка больше не засчитана, опыт за неё снимается."""
    if instance.player_id:
        _revoke(instance.user, "follow_player", instance.player_id)
    if instance.team_id:
        _revoke(instance.user, "follow_team", instance.team_id)


@receiver(post_save, sender=PushSubscription)
def on_push(sender, instance, created, **kwargs):
    if created:
        _record(instance.user, "push_enabled", "push")


@receiver(post_delete, sender=PushSubscription)
def on_push_removed(sender, instance, **kwargs):
    if not PushSubscription.objects.filter(user_id=instance.user_id).exclude(pk=instance.pk).exists():
        _revoke(instance.user, "push_enabled", "push")


@receiver(post_save, sender=TelegramAccount)
def on_telegram(sender, instance, created, **kwargs):
    if created:
        _record(instance.user, "telegram_linked", "telegram")


@receiver(post_delete, sender=TelegramAccount)
def on_telegram_removed(sender, instance, **kwargs):
    _revoke(instance.user, "telegram_linked", "telegram")


@receiver(post_save, sender=FriendLeague)
def on_league_created(sender, instance, created, **kwargs):
    if created:
        _record(instance.owner, "league_created", instance.pk)


@receiver(post_delete, sender=FriendLeague)
def on_league_deleted(sender, instance, **kwargs):
    _revoke(instance.owner, "league_created", instance.pk)


@receiver(post_save, sender=FriendLeagueMember)
def on_league_joined(sender, instance, created, **kwargs):
    """Друг вступил в лигу — засчитать владельцу «Позовите друга в свою лигу»."""
    if created and instance.user_id != instance.league.owner_id:
        _record(instance.league.owner, "league_joined", instance.user_id)


@receiver(post_delete, sender=FriendLeagueMember)
def on_league_left(sender, instance, **kwargs):
    try:
        owner = instance.league.owner
    except FriendLeague.DoesNotExist:
        return
    if instance.user_id != owner.pk:
        _revoke(owner, "league_joined", instance.user_id)


@receiver(post_save, sender=SeasonPass)
def on_season_pass_saved(sender, instance, **kwargs):
    """Уровень подняли не начислением XP (например, в админке) — награды всё равно положены."""
    from engagement.season import claim_due_rewards

    transaction.on_commit(lambda: claim_due_rewards(instance))
