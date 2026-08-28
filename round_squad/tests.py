# round_squad/tests.py
"""
"DOPX Лучшие тура" не имели ни одного теста — round_squad почти дословно
повторяет уже покрытый season_squad/tests.py, но с одним принципиальным
отличием (см. докстринг round_squad/models.py и round_squad/services.py):
байесовское сглаживание здесь идёт по ЧИСЛУ ГОЛОСОВ за единственный матч
тура (ROUND_VOTE_SHRINKAGE_C), а не по числу оценённых МАТЧЕЙ за сезон
(season_squad.SHRINKAGE_C) — в туре у игрока почти всегда ровно один матч,
поэтому число голосов там — единственный доступный сигнал надёжности.

Ниже зеркалятся только те тест-классы season_squad/tests.py, что реально
применимы к round_squad: сам алгоритм жадного распределения слотов
(байес/порог/один слот на кандидата/пустой слот), тренер тура,
Redis-lock пересчёта. Судьи в round_squad НЕТ (см. докстринг
RoundBestXISlot — "без судьи"), поэтому RefereeScoreTests не зеркалится.
Дополнительно (специфика round_squad, у season_squad аналогов нет):
"игрок тура" (плоский пул НЕЗАВИСИМО от слота формации), "самый
драматичный матч тура" (MatchEvaluation.entertainment*tension),
автоматическая фиксация тура (is_final взводится САМ, когда закрывается
голосование по всем матчам тура — в отличие от season_squad, где это
ручное действие стаффа) и resolve_practically_closed_tour (выбор "тура
по умолчанию" с терпимостью к переносам матчей).
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from aggregates.models import CoachMatchAggregate, PlayerMatchAggregate
from coaches.models import Coach
from evaluations.models import MatchEvaluation
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from round_squad.models import RoundBestXI, RoundBestXISlot
from round_squad.services import (
    ROUND_CONFIDENT_VOTES_THRESHOLD,
    ROUND_MIN_VOTES_FOR_CANDIDATE,
    _round_bayes_score,
    recompute_round,
    resolve_practically_closed_tour,
)
from round_squad.tasks import recompute_round_task, send_round_results_notification
from seasons.models import Season
from teams.models import Team
from users.models import User

LOCMEM_CACHE = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


class RoundSquadTestCase(TestCase):
    """Общая фикстура: лига/сезон/две команды + хелперы для быстрой сборки
    голосов за тур, минуя полный wizard оценки — тот же приём, что
    season_squad/tests.py::SeasonSquadTestCase."""

    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team_a = Team.objects.create(name="Team A")
        self.team_b = Team.objects.create(name="Team B")
        self._match_counter = 0
        self._user_counter = 0

    def make_match(self, tour, status='finished', start_time=None, voting_open_until=None, home_coach=None):
        self._match_counter += 1
        start_time = start_time or (timezone.now() - timedelta(days=100 - self._match_counter))
        # По умолчанию голосование ещё открыто (тур "живой") — иначе КАЖДЫЙ
        # вызов recompute_round автоматически зафиксировал бы тур (см.
        # докстринг round_squad/models.py про is_final), что мешало бы
        # тестам самого алгоритма подбора состава.
        voting_open_until = voting_open_until or (timezone.now() + timedelta(days=1))
        return Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team_a, away_team=self.team_b,
            home_coach=home_coach,
            tour=tour, status=status, start_time=start_time,
            voting_open_until=voting_open_until,
        )

    def add_to_lineup(self, match, player, team, side, position):
        lineup, _ = MatchLineup.objects.get_or_create(match=match, team=team, defaults={'side': side})
        return MatchLineupPlayer.objects.create(lineup=lineup, player=player, position=position)

    def make_player_in_round(self, name, team, position, tour, score, votes, side='home'):
        """Игрок с РОВНО одним оценённым матчем тура (реалистичный случай —
        см. докстринг round_squad/models.py) — `votes` голосов, `score`
        средняя. Отдельный матч на каждого игрока, все в одном туре."""
        match = self.make_match(tour=tour)
        first, last = name.split(" ", 1)
        player = Player.objects.create(first_name=first, last_name=last, team=team)
        self.add_to_lineup(match, player, team, side, position)
        PlayerMatchAggregate.objects.create(
            player=player, match=match, performance_score=score, total_votes=votes,
        )
        return player

    def make_user(self, name="user"):
        # БАГ, КОТОРЫЙ ТУТ БЫЛ (найден реальным прогоном против Postgres,
        # 2026-08-28): без явного email оба вызова передавали пустую строку
        # по умолчанию — User.email unique=True на уровне БД, поэтому второй
        # же вызов make_user() в тестах с несколькими "оценщиками" падал
        # IntegrityError'ом "duplicate key... users_user_email_key". В
        # SQLite/локальной песочнице без реальной Postgres это не всплывало.
        self._user_counter += 1
        return User.objects.create_user(
            username=f"{name}{self._user_counter}",
            email=f"{name}{self._user_counter}@test.local",
            password="testpass123",
        )


class RoundBestXIAlgorithmTests(RoundSquadTestCase):
    """Базовые гарантии жадного алгоритма подбора состава тура
    (services.py::recompute_round) — те же свойства, что и у
    season_squad.BestXIAlgorithmTests, только сглаживание по голосам."""

    def test_candidate_below_min_votes_is_excluded(self):
        """ROUND_MIN_VOTES_FOR_CANDIDATE=3 — 2 голоса (в т.ч. один
        троллинг-голос) не должны решать, кто в составе тура, даже с
        максимальной оценкой."""
        self.assertEqual(ROUND_MIN_VOTES_FOR_CANDIDATE, 3)
        low_votes_player = self.make_player_in_round(
            "Мало Голосов", self.team_a, "ST", tour=1, score=10.0, votes=2,
        )
        round_xi = recompute_round(self.season, tour=1)
        slot = RoundBestXISlot.objects.get(round_best_xi=round_xi, slot_code="ST")
        self.assertIsNone(slot.content_type_id)
        self.assertNotEqual(str(slot.object_id), str(low_votes_player.id))

    def test_bayes_shrinkage_prefers_more_voted_candidate(self):
        """Ядро продуктового требования round_squad/services.py: кандидат с
        БОЛЬШИМ числом голосов и чуть меньшей сырой средней должен получать
        более высокий round_score, чем кандидат с крошечным числом голосов
        (на минимально допустимых 3) и чуть более высокой сырой средней —
        зеркало season_squad.test_bayes_shrinkage_prefers_stable_large_sample,
        только единица измерения объёма данных — голоса, не матчи."""
        pool_avg = 6.0
        small_sample_score = _round_bayes_score(raw_avg=7.6, votes=3, pool_avg=pool_avg)
        large_sample_score = _round_bayes_score(raw_avg=7.5, votes=50, pool_avg=pool_avg)
        self.assertGreater(large_sample_score, small_sample_score)

    def test_round_score_shrinks_toward_pool_average(self):
        """Сглаженный скор всегда лежит СТРОГО между сырой средней и
        средним по пулу."""
        score = _round_bayes_score(raw_avg=9.5, votes=3, pool_avg=6.0)
        self.assertLess(score, 9.5)
        self.assertGreater(score, 6.0)

    def test_one_player_does_not_occupy_two_slots(self):
        """Игрок с общим кодом 'DF' (подходит сразу CB1/CB2/RB/LB) должен
        занять РОВНО один слот тура."""
        elite = self.make_player_in_round(
            "Топ Защитник", self.team_a, "DF", tour=1, score=9.0, votes=20,
        )
        for i in range(4):
            self.make_player_in_round(f"Средний Игрок{i}", self.team_a, "DF", tour=1, score=6.0, votes=5)

        round_xi = recompute_round(self.season, tour=1)
        occupied_slots = RoundBestXISlot.objects.filter(
            round_best_xi=round_xi,
            content_type=ContentType.objects.get_for_model(Player),
            object_id=elite.id,
        )
        self.assertEqual(occupied_slots.count(), 1)

    def test_empty_slot_has_no_occupant(self):
        """Позиция без единого подходящего кандидата (>=3 голосов) остаётся
        пустой, а не падает с ошибкой."""
        round_xi = recompute_round(self.season, tour=1)
        gk_slot = RoundBestXISlot.objects.get(round_best_xi=round_xi, slot_code="GK")
        self.assertIsNone(gk_slot.content_type_id)
        self.assertIsNone(gk_slot.object_id)
        self.assertEqual(gk_slot.votes_count, 0)

    def test_final_round_is_not_recomputed(self):
        """is_final=True — recompute_round должен выйти сразу, ничего не
        трогая (зеркало season_squad.test_final_xi_is_not_recomputed)."""
        round_xi = RoundBestXI.objects.create(season=self.season, tour=1, is_final=True)
        self.make_player_in_round("Новый Игрок", self.team_a, "ST", tour=1, score=9.0, votes=10)

        result = recompute_round(self.season, tour=1)

        self.assertEqual(result.pk, round_xi.pk)
        self.assertIsNone(result.last_computed_at)
        self.assertFalse(RoundBestXISlot.objects.filter(round_best_xi=round_xi).exists())


class PlayerOfRoundTests(RoundSquadTestCase):
    """«Игрок тура» — специфика round_squad без аналога в season_squad:
    плоский пул НЕЗАВИСИМО от позиции/слота формации (см. докстринг
    services.py::recompute_round про player_stats vs pool_by_code)."""

    def test_best_overall_score_wins_regardless_of_position(self):
        """Игрок сильнее на своей позиции, чем лучший игрок другой позиции,
        должен стать «игроком тура», даже если формально не входит в один
        слот с ним — ранжирование идёт по ВСЕМ кандидатам тура разом."""
        self.make_player_in_round("Средний Форвард", self.team_a, "ST", tour=1, score=7.0, votes=10)
        best_defender = self.make_player_in_round(
            "Лучший Защитник", self.team_b, "DF", tour=1, score=9.5, votes=10,
        )

        round_xi = recompute_round(self.season, tour=1)

        self.assertEqual(round_xi.player_of_round_name, best_defender.full_name)
        self.assertEqual(str(round_xi.player_of_round_object_id), str(best_defender.id))
        self.assertGreaterEqual(round_xi.player_of_round_votes, ROUND_MIN_VOTES_FOR_CANDIDATE)

    def test_player_of_round_below_min_votes_pool_is_empty(self):
        """Тот же порог ROUND_MIN_VOTES_FOR_CANDIDATE применяется и к
        плоскому пулу «игрока тура», не только к пулам слотов формации."""
        self.make_player_in_round("Один Голос", self.team_a, "ST", tour=1, score=10.0, votes=1)

        round_xi = recompute_round(self.season, tour=1)

        self.assertEqual(round_xi.player_of_round_name, "")
        self.assertIsNone(round_xi.player_of_round_content_type_id)
        self.assertEqual(round_xi.player_of_round_votes, 0)

    def test_player_of_round_empty_when_no_candidates_at_all(self):
        round_xi = recompute_round(self.season, tour=1)
        self.assertEqual(round_xi.player_of_round_name, "")
        self.assertEqual(round_xi.player_of_round_explanation, "")


class CoachOfRoundTests(RoundSquadTestCase):
    """Тренер тура — тот же порог ROUND_MIN_VOTES_FOR_CANDIDATE и то же
    кольцо доверия ROUND_CONFIDENT_VOTES_THRESHOLD, что у игроков (зеркало
    season_squad.CoachConfidenceTests, но по голосам, не матчам)."""

    def test_coach_below_min_votes_is_excluded(self):
        coach = Coach.objects.create(first_name="Один", last_name="Голос", team=self.team_a)
        match = self.make_match(tour=1, home_coach=coach)
        CoachMatchAggregate.objects.create(
            coach=coach, match=match,
            avg_tactics=9, avg_substitutions=9, avg_management=9, avg_impact=9, total_votes=2,
        )
        round_xi = recompute_round(self.season, tour=1)
        slot = RoundBestXISlot.objects.get(round_best_xi=round_xi, slot_code="COACH")
        self.assertIsNone(slot.content_type_id)

    def test_coach_confidence_flag_reflects_vote_threshold(self):
        self.assertEqual(ROUND_CONFIDENT_VOTES_THRESHOLD, 10)
        coach = Coach.objects.create(first_name="Уверенный", last_name="Тренер", team=self.team_a)
        match = self.make_match(tour=1, home_coach=coach)
        CoachMatchAggregate.objects.create(
            coach=coach, match=match,
            avg_tactics=8, avg_substitutions=8, avg_management=8, avg_impact=8,
            total_votes=ROUND_CONFIDENT_VOTES_THRESHOLD,
        )
        round_xi = recompute_round(self.season, tour=1)
        slot = RoundBestXISlot.objects.get(round_best_xi=round_xi, slot_code="COACH")
        self.assertEqual(str(slot.object_id), str(coach.id))
        self.assertTrue(slot.is_confident)

    def test_coach_below_confidence_threshold_still_occupies_slot(self):
        """5 голосов < ROUND_CONFIDENT_VOTES_THRESHOLD=10, но >=
        ROUND_MIN_VOTES_FOR_CANDIDATE=3 — тренер занимает слот, просто без
        бейджа доверия (is_confident=False), тот же принцип, что у игроков."""
        coach = Coach.objects.create(first_name="Пока", last_name="Неуверенный", team=self.team_a)
        match = self.make_match(tour=1, home_coach=coach)
        CoachMatchAggregate.objects.create(
            coach=coach, match=match,
            avg_tactics=7, avg_substitutions=7, avg_management=7, avg_impact=7, total_votes=5,
        )
        round_xi = recompute_round(self.season, tour=1)
        slot = RoundBestXISlot.objects.get(round_best_xi=round_xi, slot_code="COACH")
        self.assertEqual(str(slot.object_id), str(coach.id))
        self.assertFalse(slot.is_confident)


class DramaticMatchTests(RoundSquadTestCase):
    """«Самый драматичный матч тура» — специфика round_squad без аналога в
    season_squad: MatchEvaluation.entertainment*tension, усреднённый по
    матчу, минимум ROUND_MIN_VOTES_FOR_CANDIDATE оценок (см. докстринг
    services.py::_find_most_dramatic_match)."""

    def _evaluate_match(self, match, entertainment, tension, n_votes):
        for _ in range(n_votes):
            MatchEvaluation.objects.create(
                user=self.make_user("evaluator"), match=match,
                entertainment=entertainment, tension=tension, fairness=8,
            )

    def test_match_below_min_votes_is_excluded(self):
        """2 оценки (< ROUND_MIN_VOTES_FOR_CANDIDATE=3) — даже с максимальной
        драмой матч не должен попасть в «самый драматичный», решать по
        1-2 голосам нельзя (тот же принцип, что у кандидатов состава)."""
        match = self.make_match(tour=1)
        self._evaluate_match(match, entertainment=10, tension=10, n_votes=2)

        round_xi = recompute_round(self.season, tour=1)

        self.assertIsNone(round_xi.most_dramatic_match_id)
        self.assertEqual(round_xi.most_dramatic_match_explanation, "")

    def test_picks_match_with_highest_drama_average(self):
        dramatic_match = self.make_match(tour=1)
        self._evaluate_match(dramatic_match, entertainment=10, tension=10, n_votes=3)  # drama=100

        calm_match = self.make_match(tour=1)
        self._evaluate_match(calm_match, entertainment=3, tension=3, n_votes=3)  # drama=9

        round_xi = recompute_round(self.season, tour=1)

        self.assertEqual(round_xi.most_dramatic_match_id, dramatic_match.id)
        self.assertAlmostEqual(round_xi.most_dramatic_match_score, 100.0)
        self.assertIn(self.team_a.name, round_xi.most_dramatic_match_explanation)

    def test_no_dramatic_match_when_no_evaluations_exist(self):
        self.make_match(tour=1)
        round_xi = recompute_round(self.season, tour=1)
        self.assertIsNone(round_xi.most_dramatic_match_id)
        self.assertEqual(round_xi.most_dramatic_match_explanation, "")


class RoundAutoFinalizationTests(RoundSquadTestCase):
    """is_final взводится САМ, когда голосование по ВСЕМ матчам тура
    закрыто — принципиальное отличие от season_squad, где это ручное
    действие стаффа (см. докстринг round_squad/models.py)."""

    def test_round_finalizes_automatically_once_voting_closes_on_all_matches(self):
        past = timezone.now() - timedelta(hours=1)
        self.make_match(tour=1, voting_open_until=past)
        self.make_match(tour=1, voting_open_until=past)

        round_xi = recompute_round(self.season, tour=1)

        self.assertTrue(round_xi.is_final)
        self.assertIsNotNone(round_xi.finalized_at)

    def test_round_stays_live_while_any_match_voting_still_open(self):
        past = timezone.now() - timedelta(hours=1)
        future = timezone.now() + timedelta(hours=1)
        self.make_match(tour=1, voting_open_until=past)
        self.make_match(tour=1, voting_open_until=future)  # один матч ещё не закрыт

        round_xi = recompute_round(self.season, tour=1)

        self.assertFalse(round_xi.is_final)
        self.assertIsNone(round_xi.finalized_at)

    def test_once_finalized_a_new_candidate_is_not_picked_up(self):
        """После автофиксации следующий прогон — no-op (та же гарантия, что
        у test_final_round_is_not_recomputed, но через реальный сценарий
        закрытия голосования, а не ручную установку is_final)."""
        past = timezone.now() - timedelta(hours=1)
        self.make_match(tour=1, voting_open_until=past)
        first_run = recompute_round(self.season, tour=1)
        self.assertTrue(first_run.is_final)

        self.make_player_in_round("Опоздавший Игрок", self.team_a, "ST", tour=1, score=10.0, votes=20)
        second_run = recompute_round(self.season, tour=1)

        self.assertEqual(second_run.last_computed_at, first_run.last_computed_at)
        st_slot = RoundBestXISlot.objects.filter(round_best_xi=second_run, slot_code="ST").first()
        self.assertTrue(st_slot is None or st_slot.content_type_id is None)


class ResolvePracticallyClosedTourTests(RoundSquadTestCase):
    """resolve_practically_closed_tour — выбор тура "по умолчанию" (без
    явного номера в URL), терпимый к переносам матчей (см. подробный
    докстринг функции и историю 4 версий в round_squad/views.py::
    _resolve_latest_tour, откуда алгоритм вынесен). Порог — 0.75."""

    def make_tour_matches(self, tour, total, finished):
        for i in range(total):
            status = 'finished' if i < finished else 'scheduled'
            self.make_match(tour=tour, status=status)

    def test_returns_none_when_no_tours_have_matches(self):
        self.assertIsNone(resolve_practically_closed_tour(self.season))

    def test_returns_tour_at_exactly_the_completion_threshold(self):
        """4 из 4 (100%) — явно выше порога 0.75."""
        self.make_tour_matches(tour=5, total=4, finished=4)
        self.assertEqual(resolve_practically_closed_tour(self.season), 5)

    def test_skips_tour_below_threshold_scanning_downward_to_real_current_tour(self):
        """Ровно сценарий из докстринга: перенос ВПЕРЁД — тур 25 сыгран лишь
        на 12.5% (1 из 8, единственный заранее сыгранный перенесённый матч)
        и не должен перебивать реально идущий тур 22 (6 из 8 = 75%)."""
        self.make_tour_matches(tour=25, total=8, finished=1)
        self.make_tour_matches(tour=22, total=8, finished=6)

        self.assertEqual(resolve_practically_closed_tour(self.season), 22)

    def test_single_stuck_early_tour_does_not_block_later_tours(self):
        """Перенос НАЗАД внутри раннего тура (тур 6 сыгран на 50%, ниже
        порога) не должен помешать найти реально текущий тур 10 (100%) —
        сканирование идёт СВЕРХУ ВНИЗ и останавливается на первом же туре,
        прошедшем порог, не требуя фронтира "все туры ниже завершены"."""
        self.make_tour_matches(tour=10, total=4, finished=4)
        self.make_tour_matches(tour=6, total=8, finished=4)

        self.assertEqual(resolve_practically_closed_tour(self.season), 10)


@override_settings(CACHES=LOCMEM_CACHE)
class RecomputeLockTests(RoundSquadTestCase):
    """Redis-lock пересчёта — тот же паттерн и то же обоснование, что
    season_squad/tests.py::RecomputeLockTests: без него Celery Beat и
    ручной триггер могли одновременно пересчитать один и тот же тур."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_second_call_is_skipped_while_lock_held(self):
        lock_key = f"round_squad:recompute:{self.season.id}:1"
        self.assertTrue(cache.add(lock_key, "1", timeout=300))

        recompute_round_task(str(self.season.id), 1)

        self.assertFalse(RoundBestXI.objects.filter(season=self.season, tour=1).exists())
        cache.delete(lock_key)

    def test_lock_is_released_after_successful_run(self):
        recompute_round_task(str(self.season.id), 1)
        lock_key = f"round_squad:recompute:{self.season.id}:1"
        self.assertIsNone(cache.get(lock_key))
        self.assertTrue(RoundBestXI.objects.filter(season=self.season, tour=1).exists())

    def test_two_different_tours_of_same_season_do_not_share_a_lock(self):
        """lock_key включает номер тура — гонка возможна только у ДВУХ
        прогонов ОДНОГО И ТОГО ЖЕ тура (см. докстринг recompute_round_task)."""
        lock_key_tour_1 = f"round_squad:recompute:{self.season.id}:1"
        self.assertTrue(cache.add(lock_key_tour_1, "1", timeout=300))

        recompute_round_task(str(self.season.id), 2)  # другой тур — не заблокирован

        self.assertTrue(RoundBestXI.objects.filter(season=self.season, tour=2).exists())
        cache.delete(lock_key_tour_1)


@override_settings(CACHES=LOCMEM_CACHE)
class NotifyLockTests(RoundSquadTestCase):
    """send_round_results_notification — regression-тест на "БАГ, КОТОРЫЙ
    ТУТ БЫЛ" из докстринга самой задачи (round_squad/tasks.py): без лока
    двойной триггер (автофиксация + ручная фиксация стаффом или ретрай
    Celery) ставил fan-out рассылку дважды. Задача вызывается НАПРЯМУЮ (не
    через .delay), тот же приём, что notifications/tests.py — bind=True
    оборачивает функцию в Task.run, вызов без явного self работает так же,
    как в проде. Верифицированных пользователей в фикстуре нет — чанки
    получаются пустыми, реальных под-задач .delay() не ставится, так что
    тест не зависит от Celery/брокера."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_second_call_is_skipped_while_notify_lock_held(self):
        round_xi = RoundBestXI.objects.create(season=self.season, tour=1)
        lock_key = f"round_squad:notify:{round_xi.id}"
        self.assertTrue(cache.add(lock_key, "1", timeout=600))

        result = send_round_results_notification(str(round_xi.id))

        self.assertEqual(result, {'queued_chunks': 0, 'total_users': 0, 'skipped_locked': True})
        cache.delete(lock_key)

    def test_lock_is_released_after_run(self):
        round_xi = RoundBestXI.objects.create(season=self.season, tour=1)
        lock_key = f"round_squad:notify:{round_xi.id}"

        result = send_round_results_notification(str(round_xi.id))

        self.assertEqual(result, {'queued_chunks': 0, 'total_users': 0})
        self.assertIsNone(cache.get(lock_key))
