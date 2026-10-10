# core/live_signals.py
"""Что поднимает версию данных (core/live.py): изменения моделей контента и завершение
фоновых пересчётов, которые пишут массово (bulk_create — без сигналов моделей).
"""
from __future__ import annotations

from celery.signals import task_postrun
from django.db.models.signals import post_delete, post_save

from core.live import bump_data_version

# Модели, изменение которых видно на страницах сайта (label.Model).
LIVE_MODELS = (
    "matches.Match", "matches.MatchReaction", "matches.MatchTeamStatistics",
    "events.MatchEvent", "lineups.MatchLineup",
    "evaluations.EvaluationSession",
    "aggregates.MatchAggregate", "aggregates.RefereeMatchAggregate",
    "teams.TeamSeasonStats", "predictions.MatchPrediction",
    "season_squad.SeasonBestXI", "round_squad.RoundBestXI",
    "users.UserBadge",
)

# Фоновые задачи, после которых данные на страницах могли измениться.
LIVE_TASK_PREFIXES = (
    "aggregates.tasks.", "season_squad.tasks.", "round_squad.tasks.",
    "parsers.sportmonks.tasks.", "users.tasks.recompute_user_progress_task",
)


def _on_change(sender, **kwargs):
    bump_data_version()


# Частые опросы: версию поднимаем, только если задача вернула число изменений > 0,
# иначе каждые 15 с все открытые вкладки перезапрашивают страницу впустую.
POLL_TASKS = frozenset({"parsers.sportmonks.tasks.sportmonks_update_live"})


def _on_task_done(sender=None, task=None, retval=None, **kwargs):
    name = getattr(task, "name", "") or ""
    if name in POLL_TASKS and not retval:
        return
    if name.startswith(LIVE_TASK_PREFIXES):
        bump_data_version()


def _on_notification(sender, instance, **kwargs):
    """Новое или прочитанное уведомление — открытые вкладки пользователя обновят колокольчик за секунды."""
    from core.live import bump_user_version

    bump_user_version(instance.user_id)


def connect() -> None:
    from django.apps import apps

    notification = apps.get_model("notifications.Notification")
    post_save.connect(_on_notification, sender=notification, dispatch_uid="live-notification-save")
    post_delete.connect(_on_notification, sender=notification, dispatch_uid="live-notification-delete")

    for label in LIVE_MODELS:
        model = apps.get_model(label)
        post_save.connect(_on_change, sender=model, dispatch_uid=f"live-save-{label}")
        post_delete.connect(_on_change, sender=model, dispatch_uid=f"live-delete-{label}")
    task_postrun.connect(_on_task_done, dispatch_uid="live-task-postrun")
