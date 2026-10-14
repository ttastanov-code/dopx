# matches/management/commands/demo_match.py
"""Демо-матч для показа мастера оценки: голосование открыто, пока не закроете.

    manage.py demo_match --team "Улытау"              # последний сыгранный матч клуба с составами
    manage.py demo_match --match <uuid> --days 365    # конкретный матч
    manage.py demo_match --team "Улытау" --reset-for t.tastanov   # снять свою оценку, чтобы пройти мастер снова
    manage.py demo_match --close                      # вернуть все демо-матчи в обычное состояние

Матч помечается manual_override, иначе синхронизация вернёт окно голосования (старт + 48 ч).
Пока голосование открыто, рейтинги матча не публикуются и не входят в рейтинги игроков.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from core.management.seed_guard import ensure_seed_allowed
from matches.models import Match

# Окно открытого голосования длиннее этого — признак демо-матча (для --close).
DEMO_MIN_EXTRA = timedelta(days=3)
NORMAL_VOTING = timedelta(hours=48)


class Command(BaseCommand):
    help = "Держать матч открытым для голосования (показ мастера оценки)."

    def add_arguments(self, parser):
        parser.add_argument("--team", help="Название клуба: берётся его последний сыгранный матч с составами.")
        parser.add_argument("--match", help="UUID матча.")
        parser.add_argument("--days", type=int, default=365, help="На сколько дней открыть голосование.")
        parser.add_argument("--reset-for", dest="reset_for", help="Логин: удалить его оценку этого матча.")
        parser.add_argument("--close", action="store_true", help="Закрыть все демо-матчи.")

    def handle(self, *args, **opts):
        ensure_seed_allowed()
        if opts["close"]:
            return self._close()
        match = self._pick(opts["team"], opts["match"])
        with transaction.atomic():
            match.voting_open_until = timezone.now() + timedelta(days=opts["days"])
            match.manual_override = True
            match.save(update_fields=["voting_open_until", "manual_override", "updated_at"])
            if opts["reset_for"]:
                self._reset(match, opts["reset_for"])
        self.stdout.write(self.style.SUCCESS(
            f"Демо-матч: {match.home_team} — {match.away_team}, {timezone.localtime(match.start_time):%d.%m.%Y}\n"
            f"Голосование открыто до {timezone.localtime(match.voting_open_until):%d.%m.%Y}\n"
            f"Страница: /matches/{match.id}/"
        ))

    def _pick(self, team: str | None, match_id: str | None) -> Match:
        if match_id:
            match = Match.objects.filter(pk=match_id).select_related("home_team", "away_team").first()
            if not match:
                raise CommandError("Матч не найден.")
            if match.status != "finished":
                raise CommandError("Оценивать можно только сыгранный матч.")
            return match
        if not team:
            raise CommandError("Укажите --team или --match.")
        # Сыгранный матч клуба с обоими составами и событиями — мастеру есть что показать.
        match = (
            Match.objects.filter(Q(home_team__name__iexact=team) | Q(away_team__name__iexact=team), status="finished")
            .annotate(lineups_n=Count("lineups", distinct=True), events_n=Count("events", distinct=True))
            .filter(lineups_n__gte=2, events_n__gt=0)
            .select_related("home_team", "away_team")
            .order_by("-start_time")
            .first()
        )
        if not match:
            raise CommandError(f"У клуба «{team}» нет сыгранного матча с составами.")
        return match

    def _reset(self, match: Match, username: str) -> None:
        from django.contrib.auth import get_user_model

        from evaluations.models import (
            CoachEvaluation, ContextEvaluation, EvaluationSession, MatchEvaluation,
            PlayerEvaluation, RefereeEvaluation, TeamEvaluation,
        )

        user = get_user_model().objects.filter(username=username).first()
        if not user:
            raise CommandError(f"Пользователь «{username}» не найден.")
        removed = 0
        for model in (PlayerEvaluation, TeamEvaluation, CoachEvaluation, RefereeEvaluation,
                      MatchEvaluation, ContextEvaluation, EvaluationSession):
            removed += model.objects.filter(user=user, match=match).delete()[0]
        self.stdout.write(f"Оценка «{username}» этого матча снята ({removed} записей) — мастер можно пройти заново.")

    def _close(self) -> None:
        now = timezone.now()
        demo = [m for m in Match.objects.filter(manual_override=True, status="finished", voting_open_until__gt=now)
                if m.voting_open_until - m.start_time > NORMAL_VOTING + DEMO_MIN_EXTRA]
        for m in demo:
            m.voting_open_until = m.start_time + NORMAL_VOTING
            m.manual_override = False
            m.save(update_fields=["voting_open_until", "manual_override", "updated_at"])
            self.stdout.write(f"Закрыт: {m.home_team} — {m.away_team}")
        self.stdout.write(self.style.SUCCESS(f"Демо-матчей закрыто: {len(demo)}"))
