# core/management/commands/seed_full_history.py
"""
manage.py seed_full_history [--season-id UUID] [--tour-from N] [--tour-to N]
                             [--pool-size N] [--seed N] [--no-recalc] [--no-badges]

Продуктовый запрос (2026-09-07): "надо как-то наполнить нашу базу оценками,
прогнозами и тд за все туры которые были чтобы полноценно посмотреть
функционал как работает. Нужна прям качественная имитация" — в отличие от
`seed_match_votes` (наполняет ВЫБРАННЫЕ матчи оценками, ничего не знает про
прогнозы/серии/бейджи/XP и не трогает сборные тура/сезона), эта команда
прогоняет ВСЮ историю сезона одним связным проходом, чтобы после неё было
осмысленно смотреть не только "рейтинг игрока", но и серии, достижения,
уровень/XP, "сборную тура", "сборную сезона" и турнирную таблицу.

ЧТО ИМЕННО ДЕЛАЕТ, по каждому завершённому матчу сезона, В ПОРЯДКЕ ТУРОВ
(порядок туров критичен — evaluation_streak/prediction_streak считаются
как "подряд", см. ниже):
  1. Оценки (evaluations) — тот же реализм, что и в `seed_match_votes`
     (Gaussian вокруг "истинного качества" игрока, разный "охват" по
     игрокам), но ДОПОЛНИТЕЛЬНО создаёт `EvaluationSession(status=
     'completed')` и вызывает `User.update_evaluation_stats(match)` —
     ровно то же, что делает реальный вайзард на шаге "Матч" (см.
     `evaluations/views.py::EvaluateMatchFinalView.form_valid`). Без этого
     `total_evaluations`/`evaluation_streak` остались бы нулями и все
     завязанные на них бейджи (`active_fan_*`, `streak_*`, `foresight`,
     `full_season`, `season_completionist`, `max_trust`) никогда бы не
     выдались — ровно то, что мешало "полноценно посмотреть функционал".
  2. XP — тот же порядок величины, что и реальный вайзард (`XP_CONTEXT_STEP
     + XP_TEAMS_STEP + XP_PLAYERS_STEP_MAX + XP_COACHES_STEP +
     XP_REFEREE_STEP + XP_FINAL_STEP` = 10, умноженное на
     `user.xp_multiplier()`) — НЕ пошаговое начисление за каждый под-шаг
     (это увеличило бы число запросов в десятки раз без видимой пользы для
     "посмотреть как работает уровень/XP"), а один агрегированный вызов
     `UserXP.add_xp()` на матч.
  3. Прогнозы (predictions) — НОВОЕ, этого не было ни в одной существующей
     команде (grep `MatchPrediction` по `*/management/commands/*.py` не
     находил ничего). У каждого бота — детерминированная (не хранится в
     БД, см. `_bot_traits`) "меткость" вокруг 47% (реалистично для 1X2 в
     футболе — не 33% чистой случайности и не завышенная экспертность),
     плюс ничья выбирается заметно реже как "случайная ошибка", как и у
     живых болельщиков. После создания прогноза на уже завершённый матч —
     сразу `User.update_prediction_stats(is_correct)`, ЕДИНСТВЕННЫЙ по
     проекту вызывающий код помимо `notifications/tasks.py::
     notify_prediction_results` (см. докстринг метода) — семантика
     идентична: серия обновляется в порядке РЕАЛЬНОГО завершения матчей.
  4. Синхронный пересчёт агрегатов матча (`aggregates.tasks.recalculate_*`
     напрямую, не `.delay()` — тот же приём, что в `seed_match_votes.
     _recalculate_synchronously`, не требует поднятого Celery/Redis).
  5. После всех матчей тура — `round_squad.services.recompute_round`
     (идемпотентна, сама пропускает уже зафиксированные `is_final` туры —
     ничего не сломает в туре 24, который уже зафиксирован вручную ранее).
  6. После всех туров сезона — `season_squad.services.recompute_best_xi` и
     `aggregates.tasks.recalculate_season_standings`.
  7. В самом конце — `users.services.check_and_award_badges(user)`
     НАПРЯМУЮ (не `check_and_award_badges_task.delay(...)`) для каждого
     затронутого пользователя. Специально не через Celery-таску: она же
     создаёт `Notification`/шлёт email через `send_badge_earned_notification`
     — для сотен ботов это бессмысленный спам самому себе на несуществующий
     SMTP. Прямой вызов `check_and_award_badges()` — чистая проверка и
     `UserBadge.objects.get_or_create()`, без побочных уведомлений (тот же
     принцип "создание бота через ORM не имеет побочных эффектов", что и в
     `seed_match_votes.py`).

ПОЧЕМУ ПЕРЕИСПОЛЬЗУЕТСЯ ТОТ ЖЕ ПУЛ БОТОВ, ЧТО И `seed_match_votes.py`
(`test_user_bot_NNNN` / `test.dopx.local`): чтобы у одного и того же бота
могла накопиться связная история (оценки + прогнозы + серии + бейджи) на
ОДНОМ аккаунте, а не расползаться по нескольким несвязанным пулам, и чтобы
единственная команда очистки (`cleanup_test_users --apply`) гарантированно
сносила вообще всё — она удаляет пользователя, а не отдельные записи, все
FK (`MatchEvaluation`, `MatchPrediction`, `EvaluationSession`, `UserBadge`,
`UserXP`) каскадятся сами.

"ВОВЛЕЧЁННОСТЬ" БОТА БЕЗ НОВЫХ ПОЛЕЙ В БД: у каждого бота — своя
вероятность участвовать в КОНКРЕТНОМ матче (`engagement`, свой для оценок
И для прогнозов — реалистично, что активный болельщик активен и там, и
там) и своя "меткость" прогнозов (`accuracy`). Оба значения вычисляются
детерминированно из `username` через ОТДЕЛЬНЫЙ `random.Random(username)`,
не из глобального `random` — иначе порядок глобальных вызовов (сколько раз
дёрнули `random.random()` до этого бота в этом матче) влиял бы на трейты
бота, и результат переставал бы быть воспроизводимым по `--seed` при
малейшем изменении в другом месте кода. Так трейты бота одинаковы при
каждом запуске независимо от остального рандома — только сам факт "решил
участвовать в этом матче" зависит от `--seed`.

ЧТО СОЗНАТЕЛЬНО НЕ ДЕЛАЕТСЯ (упрощения, а не забытое):
  · `trust_score` НЕ корректируется по формуле `calculate_user_trust_adjustment`
    на каждую оценку (как в реальном вайзарде) — только начальный разброс
    0.7-1.5 при создании бота (общий с `seed_match_votes`). Формула
    завязана на отклонение от community average, которое само зависит от
    уже посчитанных агрегатов — воспроизводить это пошагово задним числом
    для тысяч строк не даёт ничего сверх уже имеющегося разброса для
    проверки анти-фрод гейтов и `max_trust`/`foresight` бейджей (у них
    порог `MAX_TRUST_THRESHOLD`/`FORESIGHT_MIN_TRUST_SCORE` — можно
    подобрать `--pool-size` и просто дождаться, пока часть ботов попадёт
    в верхний хвост начального разброса).
  · Прогнозы создаются ТОЛЬКО на уже завершённые матчи (нужен
    `match.final_result` для честного is_correct). Прогнозы на будущие/
    текущие матчи — отдельная, гораздо менее интересная для "посмотреть
    функционал" история (там просто % голосов без результата).

Как сбросить — та же команда, что и у `seed_match_votes`:
  python manage.py cleanup_test_users --apply
"""
from __future__ import annotations

import random

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from evaluations.models import (
    ContextEvaluation,
    CoachEvaluation,
    EvaluationSession,
    MatchEvaluation,
    PlayerEvaluation,
    RefereeEvaluation,
    TeamEvaluation,
)
from matches.models import Match
from players.models import Player
from predictions.models import MatchPrediction
from seasons.models import Season
from users.models import UserXP

User = get_user_model()

BOT_USERNAME_PREFIX = "test_user_bot_"
BOT_EMAIL_DOMAIN = "test.dopx.local"
WATCHED_TYPES = ["full", "full", "full", "highlights", "partial"]

# Сумма XP_CONTEXT_STEP..XP_FINAL_STEP из evaluations/views.py на момент
# написания (2+2+3+1+1+1) — держим числом здесь, а не импортируем оттуда,
# чтобы не тянуть view-модуль (с его формами/миксинами) в management-команду
# ради пяти констант; если реальные веса шагов поменяются, это упрощение
# просто станет чуть менее точным, не сломается.
EVALUATION_XP_PER_MATCH = 10

# Средняя "меткость" прогнозов 1X2 у обычного болельщика — не 33% (чистая
# случайность) и не завышенная экспертность, реалистичный ориентир для
# футбольных предсказаний. Разброс по σ=0.10 даёт часть ботов ощутимо точнее
# среднего (для stable_hand/derby_prophet/perfect_tour) и часть заметно хуже.
PREDICTION_BASE_ACCURACY = 0.47
PREDICTION_ACCURACY_STDDEV = 0.10
PREDICTION_ACCURACY_MIN = 0.28
PREDICTION_ACCURACY_MAX = 0.72

# При ошибке прогноза — ничья выбирается реже, чем вторая "не угаданная"
# сторона (1/2), как и у живых людей: угадать/промахнуться мимо ничьей
# психологически другое, чем перепутать сторону победы.
WRONG_CHOICE_WEIGHTS = {"1": 1.0, "X": 0.5, "2": 1.0}


class Command(BaseCommand):
    help = (
        "Наполняет ВСЮ историю сезона (оценки + прогнозы + серии + XP + "
        "бейджи + сборные тура/сезона + турнирная таблица) синтетическими "
        "данными переиспользуемого пула ботов — качественная имитация для "
        "полноценной проверки функционала, не единичные тестовые матчи."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--season-id", type=str, default=None,
            help="UUID сезона (по умолчанию — Season.get_primary_active()).",
        )
        parser.add_argument(
            "--tour-from", type=int, default=None,
            help="Не трогать туры МЕНЬШЕ этого номера (по умолчанию — с первого сыгранного).",
        )
        parser.add_argument(
            "--tour-to", type=int, default=None,
            help="Не трогать туры БОЛЬШЕ этого номера (по умолчанию — по последний сыгранный).",
        )
        parser.add_argument(
            "--pool-size", type=int, default=150,
            help="Размер переиспользуемого пула ботов (общий с seed_match_votes, по умолчанию 150).",
        )
        parser.add_argument(
            "--seed", type=int, default=None,
            help="Seed для random — одинаковый набор участников/прогнозов при повторном запуске.",
        )
        parser.add_argument(
            "--no-recalc", action="store_true",
            help="Не пересчитывать агрегаты/сборные/таблицу (только создать сырые оценки и прогнозы).",
        )
        parser.add_argument(
            "--no-badges", action="store_true",
            help="Не запускать финальную проверку достижений (быстрее, но бейджи не появятся).",
        )

    def handle(self, *args, **options):
        if options["seed"] is not None:
            random.seed(options["seed"])

        season = self._resolve_season(options["season_id"])
        self.stdout.write(f"Сезон: {season} (лига {season.league}).")

        matches = self._resolve_matches(season, options["tour_from"], options["tour_to"])
        if not matches:
            raise CommandError("Не найдено ни одного завершённого матча с проставленным туром в этом сезоне.")

        tours = sorted({m.tour for m in matches})
        self.stdout.write(
            f"Найдено {len(matches)} завершённых матчей, туры {tours[0]}–{tours[-1]} ({len(tours)} туров)."
        )

        pool = self._ensure_bot_pool(options["pool_size"])
        self.stdout.write(f"Пул ботов готов: {len(pool)} (префикс {BOT_USERNAME_PREFIX}*, общий с seed_match_votes).")

        touched_user_ids: set[str] = set()
        total_evaluation_voters = 0
        total_predictions = 0

        matches_by_tour: dict[int, list[Match]] = {}
        for m in matches:
            matches_by_tour.setdefault(m.tour, []).append(m)

        for tour in tours:
            tour_matches = matches_by_tour[tour]
            self.stdout.write(f"Тур {tour}: {len(tour_matches)} матч(ей)...")
            for match in tour_matches:
                n_voters, n_predictions, touched = self._seed_match(match, pool)
                total_evaluation_voters += n_voters
                total_predictions += n_predictions
                touched_user_ids |= touched
                self.stdout.write(
                    f"    {match.home_team.name} vs {match.away_team.name}: "
                    f"{n_voters} оценок, {n_predictions} прогнозов."
                )
                if not options["no_recalc"]:
                    self._recalculate_match_synchronously(str(match.id))

            if not options["no_recalc"]:
                self._recompute_round(season, tour)

        if not options["no_recalc"]:
            self._recompute_season_squad_and_standings(season)

        self.stdout.write(self.style.SUCCESS(
            f"Готово: {len(matches)} матчей, {total_evaluation_voters} голосований, "
            f"{total_predictions} прогнозов, {len(touched_user_ids)} затронутых ботов."
        ))

        if not options["no_badges"]:
            awarded_total = self._award_badges(touched_user_ids)
            self.stdout.write(self.style.SUCCESS(f"Достижений выдано: {awarded_total}."))
        else:
            self.stdout.write(self.style.WARNING("--no-badges: достижения не проверялись."))

        self.stdout.write(self.style.SUCCESS(
            "Сброс — python manage.py cleanup_test_users --apply "
            "(удаляет ботов и каскадно всё, что на них завязано)."
        ))

    # ------------------------------------------------------------------

    def _resolve_season(self, season_id: str | None) -> Season:
        if season_id:
            try:
                return Season.objects.select_related("league").get(id=season_id)
            except Season.DoesNotExist as exc:
                raise CommandError(f"Сезон {season_id!r} не найден.") from exc
        season = Season.get_primary_active()
        if season is None:
            raise CommandError("Активный сезон не найден — укажите --season-id явно.")
        return season

    def _resolve_matches(self, season: Season, tour_from: int | None, tour_to: int | None) -> list[Match]:
        qs = (
            Match.objects.filter(season=season, status="finished", tour__isnull=False)
            .select_related("home_team", "away_team", "referee", "home_coach", "away_coach")
            .order_by("tour", "start_time")
        )
        if tour_from is not None:
            qs = qs.filter(tour__gte=tour_from)
        if tour_to is not None:
            qs = qs.filter(tour__lte=tour_to)
        return list(qs)

    def _ensure_bot_pool(self, pool_size: int) -> list:
        """Идентична `seed_match_votes._ensure_bot_pool` — намеренно тот же
        префикс/домен/паттерн get_or_create, чтобы пул был ОБЩИЙ между
        командами (см. докстринг модуля)."""
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
                    "trust_score": round(random.uniform(0.7, 1.5), 2),
                },
            )
            if was_created:
                user.set_password("not-a-real-account")
                user.save(update_fields=["password"])
                UserXP.objects.get_or_create(user=user)
            created.append(user)
        return created

    def _bot_traits(self, user) -> tuple[float, float]:
        """Детерминированные "вовлечённость"/"меткость" бота — см. докстринг
        модуля. Отдельный `random.Random`, НЕ глобальный `random.seed()`."""
        r = random.Random(user.username)
        engagement = min(0.95, r.random() ** 1.7)
        accuracy = max(
            PREDICTION_ACCURACY_MIN,
            min(PREDICTION_ACCURACY_MAX, r.gauss(PREDICTION_BASE_ACCURACY, PREDICTION_ACCURACY_STDDEV)),
        )
        return engagement, accuracy

    def _seed_match(self, match: Match, pool: list) -> tuple[int, int, set[str]]:
        touched: set[str] = set()
        n_voters = self._seed_evaluations(match, pool, touched)
        n_predictions = self._seed_predictions(match, pool, touched)
        return n_voters, n_predictions, touched

    def _seed_evaluations(self, match: Match, pool: list, touched: set[str]) -> int:
        lineup_players = list(
            Player.objects.filter(matchlineupplayer__lineup__match=match).distinct()
        )
        player_quality = {p.id: random.uniform(3.0, 9.0) for p in lineup_players}
        coverage = random.uniform(0.5, 0.85)  # разный "охват" по игрокам от матча к матчу

        n_created = 0
        with transaction.atomic():
            for voter in pool:
                engagement, _accuracy = self._bot_traits(voter)
                if random.random() >= engagement:
                    continue  # этот бот "пропустил" этот матч

                # Идемпотентность: если сессия уже 'completed' — матч этим
                # ботом уже обработан в прошлом прогоне, пропускаем ЦЕЛИКОМ
                # (иначе update_evaluation_stats/add_xp удвоили бы счётчики —
                # они чистые аккумуляторы, не count() по факту, см.
                # reset_ratings_data.py про ровно эту опасность).
                session, session_created = EvaluationSession.objects.get_or_create(
                    user=voter, match=match,
                    defaults={
                        "mode": random.choice(["quick", "quick", "full"]),
                        "status": "completed",
                        "completed_steps": ["context", "teams", "players", "coaches", "referee", "match_eval"],
                        "current_step": "complete",
                        "completed_at": timezone.now(),
                    },
                )
                if not session_created:
                    touched.add(str(voter.id))
                    continue

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
                        continue
                    quality = player_quality[player.id]
                    PlayerEvaluation.objects.update_or_create(
                        user=voter, match=match, player=player,
                        defaults={
                            "contribution": _clamp(round(random.gauss(quality, 1.5)), 1, 10),
                            "risk": _clamp(round(random.gauss(10 - quality, 1.5)), 1, 10),
                            "potential": _clamp(round(random.gauss(quality, 1.5)), 1, 10),
                        },
                    )

                # Ровно то же самое, что делает реальный вайзард на
                # последнем шаге (evaluations/views.py::
                # EvaluateMatchFinalView.form_valid) — без этого серии/
                # total_evaluations/XP остались бы нулями.
                voter.update_evaluation_stats(match)
                voter.refresh_from_db()
                xp, _ = UserXP.objects.get_or_create(user=voter)
                xp.add_xp(round(EVALUATION_XP_PER_MATCH * voter.xp_multiplier()))

                touched.add(str(voter.id))
                n_created += 1
        return n_created

    def _seed_predictions(self, match: Match, pool: list, touched: set[str]) -> int:
        final_result = match.final_result
        if final_result is None:
            return 0  # см. докстринг модуля — прогнозы только на матчи с известным исходом

        n_created = 0
        # Порядок ВНУТРИ одного матча не важен для корректности серии — она
        # зависит только от порядка МАТЧЕЙ (гарантирован циклом по турам в
        # handle()), не от порядка пользователей внутри одного матча.
        with transaction.atomic():
            for predictor in pool:
                engagement, accuracy = self._bot_traits(predictor)
                if random.random() >= engagement:
                    continue

                if MatchPrediction.objects.filter(user=predictor, match=match).exists():
                    touched.add(str(predictor.id))
                    continue  # уже спрогнозировано в прошлом прогоне — не трогаем, иначе удвоим серию

                if random.random() < accuracy:
                    choice = final_result
                else:
                    others = [c for c in ("1", "X", "2") if c != final_result]
                    weights = [WRONG_CHOICE_WEIGHTS[c] for c in others]
                    choice = random.choices(others, weights=weights)[0]

                MatchPrediction.objects.create(user=predictor, match=match, choice=choice)
                is_correct = choice == final_result

                # Единственный, кроме notify_prediction_results, вызывающий
                # код — см. докстринг User.update_prediction_stats.
                predictor.update_prediction_stats(is_correct)

                touched.add(str(predictor.id))
                n_created += 1
        return n_created

    def _recalculate_match_synchronously(self, match_id: str) -> None:
        from aggregates.tasks import (
            recalculate_coach_aggregates,
            recalculate_match_aggregate,
            recalculate_player_aggregates,
            recalculate_referee_aggregates,
            recalculate_team_aggregates,
        )

        recalculate_player_aggregates(match_id)
        recalculate_coach_aggregates(match_id)
        recalculate_team_aggregates(match_id)
        recalculate_referee_aggregates(match_id)
        try:
            recalculate_match_aggregate(match_id)
        except Exception as exc:  # noqa: BLE001 — та же причина, что в seed_match_votes
            self.stdout.write(self.style.WARNING(
                f"    Пересчёт MatchAggregate для {match_id}: {exc} (не критично)."
            ))

    def _recompute_round(self, season: Season, tour: int) -> None:
        from round_squad.services import recompute_round

        try:
            recompute_round(season, tour)
        except Exception as exc:  # noqa: BLE001 — не прерываем весь бэкфилл из-за одного тура
            self.stdout.write(self.style.WARNING(f"  Пересчёт сборной тура {tour}: {exc}"))

    def _recompute_season_squad_and_standings(self, season: Season) -> None:
        from aggregates.tasks import recalculate_season_standings
        from season_squad.services import recompute_best_xi

        try:
            recompute_best_xi(season)
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"Пересчёт сборной сезона: {exc}"))
        try:
            recalculate_season_standings(season_id=str(season.id))
        except Exception as exc:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"Пересчёт турнирной таблицы: {exc}"))

    def _award_badges(self, user_ids: set[str]) -> int:
        from users.services import check_and_award_badges

        awarded_total = 0
        for user in User.objects.filter(id__in=user_ids):
            awarded = check_and_award_badges(user)
            awarded_total += len(awarded)
        return awarded_total


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
