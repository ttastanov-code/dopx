# players/services.py
"""
merge_players() — вынесено из players/management/commands/
merge_duplicate_players.py (2026-09-22) в переиспользуемый сервис, чтобы
ОДНУ и ту же логику слияния мог звать и CLI (для разового ручного разбора
через терминал), и веб-вьюха dashboard/views.py::duplicate_players_merge
(2026-09-22, прямая просьба пользователя: "пздц это муторно копировать,
вставлять... надо оптимизировать" — слияние дублей теперь одна кнопка в
очереди "Дубли игроков", id берутся из уже существующего флага
PotentialDuplicatePlayer, руками их вводить/копировать больше не нужно).

merge_players выполняется СИНХРОННО (не через Celery) — это несколько
быстрых DB-запросов, не часовой прогон внешнего API, лишний асинхронный
хоп через очередь/поллинг статуса тут не нужен и только добавил бы
ту самую "крутится, пока не обновишь страницу" задержку, от которой
пользователь и просил уйти.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from aggregates.models import PlayerMatchAggregate, PlayerRatingCorrection
from evaluations.models import PlayerEvaluation
from events.models import MatchEvent
from lineups.models import MatchLineupPlayer
from matches.models import MatchPlayerStatistics
from players.models import Player, PlayerSidelined, PotentialDuplicatePlayer
from users.models import Follow


@dataclass
class MergeReport:
    lines: list[str] = field(default_factory=list)

    def add(self, text: str) -> None:
        self.lines.append(text)

    def __str__(self) -> str:
        return "\n".join(self.lines)


def merge_players(keep: Player, merge: Player, *, apply: bool) -> MergeReport:
    """Переносит связанные данные с `merge` на `keep`; при apply=True
    реально сохраняет изменения и удаляет `merge` в конце (apply=False —
    только отчёт, для dry-run CLI). См. подробный разбор КАЖДОЙ связи и
    почему рискованные (с UniqueConstraint на игрока) переносятся
    построчно, а не единым bulk .update(), в докстринге старой версии —
    players/management/commands/merge_duplicate_players.py."""
    report = MergeReport()
    report.add(f"Оставляем: {keep.full_name} (id={keep.id}, sportmonks_id={keep.sportmonks_id})")
    report.add(f"Сливаем: {merge.full_name} (id={merge.id}, sportmonks_id={merge.sportmonks_id})")

    with transaction.atomic():
        safe_relations = [
            ("Составы на матч", MatchLineupPlayer.objects.filter(player=merge), {"player": keep}),
            ("События (голы/карточки)", MatchEvent.objects.filter(player=merge), {"player": keep}),
            ("Ассисты", MatchEvent.objects.filter(assist_player=merge), {"assist_player": keep}),
            ("Замены (вышел с поля)", MatchEvent.objects.filter(player_out=merge), {"player_out": keep}),
            ("Недоступность (травмы/дисквалификации)", PlayerSidelined.objects.filter(player=merge), {"player": keep}),
        ]
        for label, qs, update_kwargs in safe_relations:
            count = qs.count()
            if apply and count:
                qs.update(**update_kwargs)
            report.add(f"{label}: {count}")

        risky_relations = [
            ("Оценки пользователей", PlayerEvaluation.objects.filter(player=merge), "player", ["user_id", "match_id"]),
            ("Агрегаты рейтинга за матч", PlayerMatchAggregate.objects.filter(player=merge), "player", ["match_id"]),
            ("Объективная статистика за матч", MatchPlayerStatistics.objects.filter(player=merge), "player", ["match_id"]),
        ]
        for label, qs, field_name, conflict_fields in risky_relations:
            model = qs.model
            rows = list(qs)
            moved = skipped = 0
            for row in rows:
                lookup = {f: getattr(row, f) for f in conflict_fields}
                lookup[field_name] = keep
                if model.objects.filter(**lookup).exists():
                    skipped += 1
                    if apply:
                        row.delete()
                else:
                    moved += 1
                    if apply:
                        setattr(row, field_name, keep)
                        row.save(update_fields=[field_name])
            report.add(f"{label}: перенесено {moved}, конфликтов удалено {skipped}")

        correction = PlayerRatingCorrection.objects.filter(player=merge).first()
        if correction is not None:
            keep_has_correction = PlayerRatingCorrection.objects.filter(player=keep).exists()
            if keep_has_correction:
                report.add("Поправка рейтинга (авто): есть у обоих — оставлена у keep, у merge удалена")
                if apply:
                    correction.delete()
            else:
                report.add("Поправка рейтинга (авто): перенесена")
                if apply:
                    correction.player = keep
                    correction.save(update_fields=["player"])

        follow_rows = list(Follow.objects.filter(player=merge))
        follow_moved = follow_skipped = 0
        for f in follow_rows:
            if Follow.objects.filter(user_id=f.user_id, player=keep).exists():
                follow_skipped += 1
                if apply:
                    f.delete()
            else:
                follow_moved += 1
                if apply:
                    f.player = keep
                    f.save(update_fields=["player"])
        report.add(f"Подписки пользователей: перенесено {follow_moved}, конфликтов удалено {follow_skipped}")

        if apply:
            PotentialDuplicatePlayer.objects.filter(
                existing_player__in=[keep, merge], new_player__in=[keep, merge],
            ).update(reviewed=True, note="Объединено через players.services.merge_players().")
            merge_full_name, merge_id = merge.full_name, merge.id
            merge.delete()
            report.add(f"Готово: {merge_full_name} (id={merge_id}) удалён.")
        else:
            report.add("Dry-run — ничего не изменено.")

    return report
