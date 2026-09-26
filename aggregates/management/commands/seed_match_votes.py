# aggregates/management/commands/seed_match_votes.py
"""manage.py seed_match_votes --match-id X [--match-id Y ...] [--voters N] [...]
manage.py seed_match_votes --status finished --limit N [...]

Синтетические голоса ботов для матчей: общий пул test_user_bot_NNNN (создаётся один раз),
разная явка по игрокам, распределение по лагерям, синхронный пересчёт агрегатов.
Боты создаются через ORM — без писем.

Примеры:
  python manage.py seed_match_votes --match-id <uuid> --voters 40
  python manage.py seed_match_votes --status finished --limit 5 --voters 15
  python manage.py seed_match_votes --match-id <uuid> --voters 3
  python manage.py seed_match_votes --match-id <uuid> --voters 60 --single-inflated-player <player_uuid>

Сброс:
  python manage.py cleanup_test_users --apply
  python manage.py recalculate_aggregates --all-active
"""
from __future__ import annotations

import random
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from evaluations.models import (
    ContextEvaluation,
    EvaluationSession,
    CoachEvaluation,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from lineups.models import MatchLineupPlayer
from matches.models import Match
from players.models import Player
from users.models import UserXP
from core.management.seed_guard import ensure_seed_allowed

User = get_user_model()

BOT_USERNAME_PREFIX = "test_user_bot_"
BOT_EMAIL_DOMAIN = "test.dopx.local"
WATCHED_TYPES = ["full", "full", "full", "highlights", "partial"]  # чаще "full"

def _complete_session(user, match) -> None:
    """Завершённая сессия вайзарда (ретро-время — без флага «слишком быстро»)."""
    now = timezone.now()
    session, _ = EvaluationSession.objects.get_or_create(user=user, match=match)
    EvaluationSession.objects.filter(pk=session.pk).update(
        status="completed", completed_at=now, started_at=now - timedelta(minutes=3),
        completed_steps=["context", "teams", "players", "coaches", "referee", "match_eval"],
        trust_settled_at=now,
    )


class Command(BaseCommand):
    help = (
        "Наполняет матчи синтетическими голосами (переиспользуемый пул ботов, "
        "без email/регистрации) — для проверки отображения рейтингов при разном числе оценок."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--match-id", action="append", dest="match_ids", default=[],
            help="UUID матча (можно указать несколько раз — по одному флагу на матч).",
        )
        parser.add_argument(
            "--status", type=str, default=None,
            help="Вместо --match-id: взять матчи с этим статусом (обычно 'finished').",
        )
        parser.add_argument(
            "--limit", type=int, default=10,
            help="Сколько матчей брать при использовании --status (по умолчанию 10, самые свежие).",
        )
        parser.add_argument(
            "--voters", type=int, default=20,
            help="Сколько ботов из пула проголосует за КАЖДЫЙ выбранный матч (по умолчанию 20).",
        )
        parser.add_argument(
            "--pool-size", type=int, default=300,
            help="Размер переиспользуемого пула ботов (по умолчанию 300, создаётся один раз).",
        )
        parser.add_argument(
            "--player-coverage", type=float, default=0.7,
            help="Доля голосующих, которые оценивают КАЖДОГО конкретного игрока (0.0-1.0, "
                 "по умолчанию 0.7) — создаёт естественный разброс числа голосов между игроками "
                 "одного матча, а не одинаковое total_votes у всех.",
        )
        parser.add_argument(
            "--single-inflated-player", type=str, default=None,
            help="UUID игрока — добавить ОДИН отдельный экстремальный голос (10/1/10) от "
                 "отдельного бота, вне обычного пула — проверка анти-фрод гейта "
                 "(MIN_VOTES_FOR_DISPLAY, docs/adr/0026) и single-vote-инфляции.",
        )
        parser.add_argument(
            "--seed", type=int, default=None,
            help="Seed для random — одинаковый набор голосов при повторном запуске с тем же seed.",
        )
        parser.add_argument(
            "--no-recalc", action="store_true",
            help="Не пересчитывать агрегаты синхронно после сидирования (по умолчанию пересчитывает).",
        )

    def handle(self, *args, **options):
        ensure_seed_allowed()
        if options["seed"] is not None:
            random.seed(options["seed"])

        matches = self._resolve_matches(options)
        if not matches:
            raise CommandError("Не найдено ни одного матча (проверьте --match-id/--status).")

        voters_per_match = options["voters"]
        pool_size = max(options["pool_size"], voters_per_match)
        coverage = options["player_coverage"]
        if not 0.0 < coverage <= 1.0:
            raise CommandError("--player-coverage должен быть в (0.0, 1.0].")

        pool = self._ensure_bot_pool(pool_size)
        self.stdout.write(f"Пул ботов готов: {len(pool)} (префикс {BOT_USERNAME_PREFIX}*).")

        single_inflated_player = None
        if options["single_inflated_player"]:
            try:
                single_inflated_player = Player.objects.get(id=options["single_inflated_player"])
            except Player.DoesNotExist as exc:
                raise CommandError(f"Игрок {options['single_inflated_player']!r} не найден.") from exc

        touched_match_ids: list[str] = []
        for match in matches:
            n_created = self._seed_match(
                match, pool, voters_per_match, coverage, single_inflated_player
            )
            touched_match_ids.append(str(match.id))
            self.stdout.write(
                f"  {match.home_team.name} vs {match.away_team.name} ({match.id}): "
                f"{n_created} голосующих обработано."
            )

        if not options["no_recalc"]:
            self._recalculate_synchronously(touched_match_ids)
            self.stdout.write(self.style.SUCCESS("Агрегаты пересчитаны синхронно — обновления видны сразу."))
        else:
            self.stdout.write(self.style.WARNING(
                "--no-recalc: агрегаты не пересчитаны. Запустите "
                "`python manage.py recalculate_aggregates --match-id <id>` для каждого матча, "
                "либо дождитесь планового прогона Celery."
            ))

        self.stdout.write(self.style.SUCCESS(
            f"Готово: {len(matches)} матч(ей) наполнено голосами. "
            f"Сброс — см. докстринг команды (cleanup_test_users --apply / reset_ratings_data --keep-user)."
        ))

    # ------------------------------------------------------------------

    def _resolve_matches(self, options) -> list[Match]:
        if options["match_ids"]:
            matches = list(
                Match.objects.filter(id__in=options["match_ids"]).select_related("home_team", "away_team")
            )
            missing = set(options["match_ids"]) - {str(m.id) for m in matches}
            if missing:
                raise CommandError(f"Матчи не найдены: {', '.join(missing)}")
            return matches

        qs = Match.objects.select_related("home_team", "away_team").order_by("-start_time")
        if options["status"]:
            qs = qs.filter(status=options["status"])
        return list(qs[: options["limit"]])

    def _ensure_bot_pool(self, pool_size: int) -> list:
        """Идемпотентно: создаёт только недостающих ботов."""
        existing = list(User.objects.filter(username__startswith=BOT_USERNAME_PREFIX).order_by("username"))
        if len(existing) >= pool_size:
            return existing[:pool_size]

        created = list(existing)
        for i in range(len(existing) + 1, pool_size + 1):
            username = f"{BOT_USERNAME_PREFIX}{i:04d}"
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@{BOT_EMAIL_DOMAIN}",
                    "is_verified": True,
                    # Разброс trust_score — разные веса голосов.
                    "trust_score": round(random.uniform(0.7, 1.5), 2),
                },
            )
            if was_created:
                user.set_password("not-a-real-account")
                user.save(update_fields=["password"])
                UserXP.objects.get_or_create(user=user)
            created.append(user)
        return created

    def _seed_match(self, match: Match, pool: list, voters_per_match: int, coverage: float, single_inflated_player) -> int:
        voters = random.sample(pool, k=min(voters_per_match, len(pool)))
        lineup_players = list(
            Player.objects.filter(matchlineupplayer__lineup__match=match).distinct()
        )
        home_player_ids = set(
            MatchLineupPlayer.objects.filter(lineup__match=match, lineup__team=match.home_team)
            .values_list("player_id", flat=True)
        )

        # «Истинное качество» игрока на матч, голоса — шум вокруг него.
        player_quality = {p.id: random.uniform(3.0, 9.0) for p in lineup_players}

        with transaction.atomic():
            for voter in voters:
                supported_team = random.choices(
                    [match.home_team, match.away_team, None], weights=[0.4, 0.4, 0.2]
                )[0]
                ContextEvaluation.objects.update_or_create(
                    user=voter, match=match,
                    defaults={
                        "watched_type": random.choice(WATCHED_TYPES),
                        "attended_stadium": random.random() < 0.15,
                        "supported_team": supported_team,
                    },
                )
                # В рейтинг идут только голоса завершённых сессий.
                _complete_session(voter, match)
                MatchEvaluation.objects.update_or_create(
                    user=voter, match=match,
                    defaults={
                        "entertainment": _clamp(round(random.gauss(6.5, 2.0)), 1, 10),
                        "tension": _clamp(round(random.gauss(6.0, 2.0)), 1, 10),
                        "fairness": _clamp(round(random.gauss(6.5, 2.0)), 1, 10),
                        "turning_point": random.random() < 0.25,
                    },
                )
                if match.referee_id:
                    RefereeEvaluation.objects.update_or_create(
                        user=voter, match=match,
                        defaults={
                            "influence_score": _clamp(round(random.gauss(35, 20)), 0, 100),
                            "decision_quality": _clamp(round(random.gauss(6.5, 2.0)), 1, 10),
                        },
                    )
                for team in (match.home_team, match.away_team):
                    TeamEvaluation.objects.update_or_create(
                        user=voter, match=match, team=team,
                        defaults={
                            "tactics": _clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                            "effort": _clamp(round(random.gauss(6.8, 1.8)), 1, 10),
                            "organization": _clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                            "mentality": _clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                        },
                    )
                for coach in (match.home_coach, match.away_coach):
                    if coach is None:
                        continue
                    CoachEvaluation.objects.update_or_create(
                        user=voter, match=match, coach=coach,
                        defaults={
                            "tactics": _clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                            "substitutions": _clamp(round(random.gauss(6.0, 1.8)), 1, 10),
                            "game_management": _clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                            "impact": _clamp(round(random.gauss(6.5, 1.8)), 1, 10),
                        },
                    )
                for player in lineup_players:
                    if random.random() > coverage:
                        continue  # бот пропустил игрока
                    quality = player_quality[player.id]
                    PlayerEvaluation.objects.update_or_create(
                        user=voter, match=match, player=player,
                        defaults={
                            "contribution": _clamp(round(random.gauss(quality, 1.5)), 1, 10),
                            "risk": _clamp(round(random.gauss(10 - quality, 1.5)), 1, 10),
                            "potential": _clamp(round(random.gauss(quality, 1.5)), 1, 10),
                        },
                    )

            if single_inflated_player is not None:
                self._add_single_inflated_vote(match, single_inflated_player)

        return len(voters)

    def _add_single_inflated_vote(self, match: Match, player: Player) -> None:
        """Отдельный бот с одним экстремальным голосом — проверка порога голосов."""
        username = f"{BOT_USERNAME_PREFIX}inflated_{match.id.hex[:8]}_{player.id.hex[:8]}"
        bot, _ = User.objects.get_or_create(
            username=username,
            defaults={"email": f"{username}@{BOT_EMAIL_DOMAIN}", "is_verified": True},
        )
        ContextEvaluation.objects.update_or_create(
            user=bot, match=match, defaults={"watched_type": "full"}
        )
        _complete_session(bot, match)
        PlayerEvaluation.objects.update_or_create(
            user=bot, match=match, player=player,
            defaults={"contribution": 10, "risk": 1, "potential": 10},
        )
        self.stdout.write(self.style.WARNING(
            f"  + единичный накрученный голос за игрока {player} (10/1/10, total_votes должен остаться 1)."
        ))

    def _recalculate_synchronously(self, match_ids: list[str]) -> None:
        """Синхронный пересчёт без Celery. Ошибка .delay() внутри — только в лог."""
        from aggregates.tasks import (
            recalculate_coach_aggregates,
            recalculate_match_aggregate,
            recalculate_player_aggregates,
            recalculate_referee_aggregates,
            recalculate_team_aggregates,
        )

        for match_id in match_ids:
            recalculate_player_aggregates(match_id)
            recalculate_coach_aggregates(match_id)
            recalculate_team_aggregates(match_id)
            recalculate_referee_aggregates(match_id)
            try:
                recalculate_match_aggregate(match_id)
            except Exception as exc:  # noqa: BLE001 — см. докстринг метода
                self.stdout.write(self.style.WARNING(
                    f"  Пересчёт MatchAggregate для {match_id}: {exc} "
                    f"(PlayerMatchAggregate уже пересчитан синхронно строкой выше — не критично)."
                ))


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
