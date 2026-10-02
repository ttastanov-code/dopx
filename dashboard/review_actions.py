# dashboard/review_actions.py
"""Разбор «Проверки ФИО» и «Дублей игроков» — общий для дашборда и бота.
Возвращают Result; аудит пишет вызывающий код."""
from __future__ import annotations

from dataclasses import dataclass, field

from django.utils import timezone

from core.models import NAME_SOURCE_AI_VERIFIED
from parsers.models import ConfirmedNameCorrection
from players.services import merge_players

from .models import AuditAction


@dataclass
class Result:
    ok: bool
    message: str
    action: str = ""
    target: str = ""
    details: dict = field(default_factory=dict)


def _close(suggestion, user, status):
    suggestion.status = status
    suggestion.reviewed_by = user
    suggestion.reviewed_at = timezone.now()
    suggestion.save(update_fields=["status", "reviewed_by", "reviewed_at", "updated_at"])


def reject_name(suggestion, user) -> Result:
    """Отклонить; техническую ошибку Gemini — удалить, чтобы сущность проверилась заново."""
    if suggestion.status not in ("pending_review", "check_failed"):
        return Result(False, "Это предложение уже разобрано.")
    target = f"{suggestion.entity_label}:{suggestion.object_id}"
    current = f"{suggestion.current_first_name} {suggestion.current_last_name}"
    if suggestion.status == "check_failed":
        details = {"current": current, "dismissed_error": suggestion.error_message}
        suggestion.delete()
        return Result(True, "Ошибка скрыта — запись сама попадёт под проверку в следующем обычном прогоне (без --recheck).",
                      AuditAction.NAME_SUGGESTION_REJECTED, target, details)
    _close(suggestion, user, "rejected")
    return Result(True, "Предложение отклонено.", AuditAction.NAME_SUGGESTION_REJECTED, target, {"current": current})


def approve_name(suggestion, user, first: str | None = None, last: str | None = None) -> Result:
    """Принять: пишет ConfirmedNameCorrection и сразу обновляет сущность. first/last — правка сотрудника."""
    if suggestion.status not in ("pending_review", "check_failed"):
        return Result(False, "Это предложение уже разобрано.")
    final_first = (first or suggestion.suggested_first_name or "").strip()
    final_last = (last or suggestion.suggested_last_name or "").strip()
    if not final_first and not final_last:
        return Result(False, "Пустое имя и фамилия — нечего подтверждать.")
    entity = suggestion.content_object
    if entity is None:
        _close(suggestion, user, "rejected")
        return Result(False, "Сущность (игрок/судья/тренер) больше не существует — подтвердить нечего.")

    old_first, old_last = entity.first_name, entity.last_name
    update_fields = []
    for attr, final in (("first_name", final_first), ("last_name", final_last)):
        current = getattr(entity, attr)
        if final and final != current:
            ConfirmedNameCorrection.objects.update_or_create(
                wrong_text=current.strip().lower(),
                defaults={"correct_text": final, "source_suggestion": suggestion, "created_by": user},
            )
            setattr(entity, attr, final)
            update_fields.append(attr)
    if update_fields:
        entity.name_source = NAME_SOURCE_AI_VERIFIED
        entity.save(update_fields=update_fields + ["name_source", "updated_at"])
    _close(suggestion, user, "approved")
    return Result(True, f"Подтверждено: {old_first} {old_last} → {final_first} {final_last}",
                  AuditAction.NAME_SUGGESTION_APPROVED, f"{suggestion.entity_label}:{suggestion.object_id}",
                  {"was": f"{old_first} {old_last}", "now": f"{final_first} {final_last}"})


def merge_duplicate(flag, keep_side: str, user) -> Result:
    """keep_side=existing|new — какую запись оставить; вторая сливается в неё и удаляется."""
    if keep_side not in ("existing", "new"):
        return Result(False, f"Неизвестное значение keep: {keep_side}")
    if flag.reviewed:
        return Result(False, "Этот флаг уже разобран.")
    keep, merge = (flag.existing_player, flag.new_player) if keep_side == "existing" else (flag.new_player, flag.existing_player)
    keep_name, keep_id, merge_name, merge_id = keep.full_name, keep.id, merge.full_name, merge.id
    report = merge_players(keep, merge, apply=True)
    return Result(True, f"Объединено: {merge_name} → {keep_name}", AuditAction.DUPLICATE_PLAYERS_MERGED, f"player:{keep_id}",
                  {"kept": f"{keep_name} ({keep_id})", "merged": f"{merge_name} ({merge_id})", "report": report.lines})


def dismiss_duplicate(flag, user) -> Result:
    """Не дубль — разные люди."""
    if flag.reviewed:
        return Result(False, "Этот флаг уже разобран.")
    flag.reviewed = True
    flag.reviewed_by = user
    flag.reviewed_at = timezone.now()
    flag.note = "Отклонено вручную: разные люди."
    flag.save(update_fields=["reviewed", "reviewed_by", "reviewed_at", "note", "updated_at"])
    return Result(True, "Отклонено — это разные люди.", AuditAction.DUPLICATE_PLAYER_FLAG_DISMISSED,
                  f"player:{flag.existing_player_id}", {"existing": str(flag.existing_player_id), "new": str(flag.new_player_id)})
