# season_squad/tests.py
"""
Минимальный набор тестов "Живой сборной сезона" — продуктовое ревью
2026-08-22 отдельно указало на отсутствие tests.py у season_squad как
главный технический риск (регрессии в байесовской логике/распределении
слотов сложно поймать вручную). Фикстуры строятся напрямую через
PlayerMatchAggregate/CoachMatchAggregate/RefereeMatchAggregate/
MatchLineupPlayer — минуя полный wizard оценки (тот же подход, что и в
aggregates/tests.py), это быстрее и точнее целится в конкретные числа.

2026-08-23: RefereeScoreTests раньше строил фикстуры через
RefereeEvaluation/MatchEvaluation и полагался на то, что _build_referee_pool
сама считает формулу 0.6*decision+0.3*fairness+0.1*(10-influence/10) на
лету. Формула переехала в aggregates/tasks.py::recalculate_referee_aggregates
(anti-brigading — единый взвешенный движок теперь и для судей), поэтому
тест обновлён строить RefereeMatchAggregate НАПРЯМУЮ — тот же приём, что
add_player_aggregate уже использовал для игроков.
"""
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from aggregates.models import CoachMatchAggregate, PlayerMatchAggregate, RefereeMatchAggregate
from aggregates.services import CONFIDENT_VOTES_THRESHOLD
from coaches.models import Coach
from leagues.models import League
from lineups.models import MatchLineup, MatchLineupPlayer
from matches.models import Match
from players.models import Player
from referees.models import Referee
from season_squad.models import SeasonBestXI, SeasonBestXISlot
from season_squad.services import MIN_MATCHES_FOR_CANDIDATE, _bayes_score, recompute_best_xi
from season_squad.tasks import recompute_best_xi_task
from seasons.models import Season
from teams.models import Team

LOCMEM_CACHE = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


class SeasonSquadTestCase(TestCase):
    """Общая фикстура: лига/сезон/две команды + хелперы для быстрой сборки
    состава и агрегатов на матч, минуя полный wizard оценки."""

    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team_a = Team.objects.create(name="Team A")
        self.team_b = Team.objects.create(name="Team B")
        self._match_counter = 0

    def make_match(self, home=None, away=None, referee=None, start_time=None):
        """start_time по умолчанию сдвигается назад с каждым вызовом, чтобы
        матчи сезона были упорядочены по времени, не совпадали день в
        день — важно для тестов "исторического клуба" (самый ПОЗДНИЙ матч)."""
        self._match_counter += 1
        return Match.objects.create(
            league=self.league,
            season=self.season,
            home_team=home or self.team_a,
            away_team=away or self.team_b,
            referee=referee,
            start_time=start_time or (timezone.now() - timedelta(days=100 - self._match_counter)),
            voting_open_until=timezone.now() - timedelta(days=1),
        )

    def add_to_lineup(self, match, player, team, side, position):
        lineup, _ = MatchLineup.objects.get_or_create(match=match, team=team, defaults={'side': side})
        return MatchLineupPlayer.objects.create(lineup=lineup, player=player, position=position)

    def add_player_aggregate(self, player, match, score, votes=5):
        return PlayerMatchAggregate.objects.create(
            player=player, match=match, performance_score=score, total_votes=votes,
        )

    def make_player_with_matches(self, name, team, position, scores, votes=5, side='home'):
        """Создаёт игрока с N оценёнными матчами сезона (по одному на каждое
        число из `scores`) — сразу с составом (для позиции/клуба) и
        агрегатом (для рейтинга). Возвращает Player."""
        first, last = name.split(" ", 1)
        player = Player.objects.create(first_name=first, last_name=last, team=team)
        for score in scores:
            match = self.make_match()
            self.add_to_lineup(match, player, team, side, position)
            self.add_player_aggregate(player, match, score, votes)
        return player


class BestXIAlgorithmTests(SeasonSquadTestCase):
    """Базовые гарантии алгоритма подбора состава (services.py::recompute_best_xi)."""

    def test_player_below_min_matches_is_excluded(self):
        """Игрок с одним оценённым матчем (< MIN_MATCHES_FOR_CANDIDATE) не
        попадает в подбор состава, даже с максимальной оценкой."""
        self.assertEqual(MIN_MATCHES_FOR_CANDIDATE, 2)
        one_match_player = self.make_player_with_matches(
            "Супер Однаоценка", self.team_a, "ST", scores=[10.0],
        )
        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="ST")
        self.assertFalse(slot.filled if hasattr(slot, "filled") else slot.content_type_id is not None)
        self.assertNotEqual(str(slot.object_id), str(one_match_player.id))

    def test_bayes_shrinkage_prefers_stable_large_sample(self):
        """Ядро продуктового требования (см. докстринг season_squad/services.py):
        игрок с БОЛЬШОЙ выборкой и чуть меньшей сырой средней должен
        получать более высокий байес-скор, чем игрок с крошечной
        выборкой (на минимально допустимых 2 матчах) и чуть более высокой
        сырой средней, если оба заметно выше среднего по пулу."""
        pool_avg = 6.0
        small_sample_score = _bayes_score(raw_avg=7.6, matches=2, pool_avg=pool_avg)
        large_sample_score = _bayes_score(raw_avg=7.5, matches=100, pool_avg=pool_avg)
        self.assertGreater(large_sample_score, small_sample_score)

    def test_bayes_score_shrinks_toward_pool_average(self):
        """Сглаженный скор всегда лежит СТРОГО между сырой средней и
        средним по пулу — базовое свойство формулы."""
        score = _bayes_score(raw_avg=9.5, matches=2, pool_avg=6.0)
        self.assertLess(score, 9.5)
        self.assertGreater(score, 6.0)

    def test_one_player_does_not_occupy_two_slots(self):
        """Игрок с общей позицией ('DF' подходит сразу CB1/CB2/RB/LB —
        см. SLOT_PROCESSING_ORDER) должен занять РОВНО один слот."""
        elite = self.make_player_with_matches(
            "Топ Защитник", self.team_a, "DF", scores=[9.0, 9.0, 9.0], votes=20,
        )
        # Средний "фоновый" защитник — чтобы у соседних слотов был хоть
        # какой-то кандидат и слоты не пустовали независимо от elite.
        for i in range(4):
            self.make_player_with_matches(f"Средний Игрок{i}", self.team_a, "DF", scores=[6.0, 6.0])

        best_xi = recompute_best_xi(self.season)
        occupied_slots = SeasonBestXISlot.objects.filter(
            best_xi=best_xi,
            content_type=ContentType.objects.get_for_model(Player),
            object_id=elite.id,
        )
        self.assertEqual(occupied_slots.count(), 1)

    def test_empty_slot_has_no_occupant(self):
        """Позиция без единого подходящего кандидата (>=2 матчей) остаётся
        пустой, а не падает с ошибкой."""
        best_xi = recompute_best_xi(self.season)
        gk_slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="GK")
        self.assertIsNone(gk_slot.content_type_id)
        self.assertIsNone(gk_slot.object_id)
        self.assertEqual(gk_slot.matches_count, 0)

    def test_final_xi_is_not_recomputed(self):
        """is_final=True — recompute_best_xi должен выйти сразу, ничего не
        трогая (используется после закрытия сезона, см. admin.py::mark_as_final)."""
        best_xi = SeasonBestXI.objects.create(season=self.season, is_final=True)
        self.make_player_with_matches("Новый Игрок", self.team_a, "ST", scores=[9.0, 9.0])

        result = recompute_best_xi(self.season)

        self.assertEqual(result.pk, best_xi.pk)
        self.assertIsNone(result.last_computed_at)
        self.assertFalse(SeasonBestXISlot.objects.filter(best_xi=best_xi).exists())

    def test_rank_change_new_then_same_then_up(self):
        """New -> Same -> Up — полный жизненный цикл стрелки изменения
        места в составе на позиции ST."""
        player_a = self.make_player_with_matches("Игрок А", self.team_a, "ST", scores=[8.0, 8.0, 8.0])

        # --- Раунд 1: единственный кандидат ---
        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="ST")
        self.assertEqual(str(slot.object_id), str(player_a.id))
        self.assertEqual(slot.rank_change, SeasonBestXISlot.RANK_CHANGE_NEW)

        # --- Раунд 2: появился игрок B слабее A — A остаётся №1 ---
        player_b = self.make_player_with_matches("Игрок Б", self.team_b, "ST", scores=[7.0, 7.0, 7.0])
        recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="ST")
        self.assertEqual(str(slot.object_id), str(player_a.id))
        self.assertEqual(slot.rank_change, SeasonBestXISlot.RANK_CHANGE_SAME)

        # --- Раунд 3: у B резко выросла выборка с высокой средней — B обгоняет A ---
        for _ in range(5):
            match = self.make_match()
            self.add_to_lineup(match, player_b, self.team_b, 'home', 'ST')
            self.add_player_aggregate(player_b, match, 9.5)

        recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="ST")
        self.assertEqual(str(slot.object_id), str(player_b.id))
        self.assertEqual(slot.rank_change, SeasonBestXISlot.RANK_CHANGE_UP)
        self.assertEqual(slot.rank_change_delta, 1)  # был на месте #2, стал #1


class HistoricalTeamNameTests(SeasonSquadTestCase):
    """Продуктовое ревью 2026-08-22: карточка должна показывать клуб, за
    который игрок реально играл В ЭТОМ СЕЗОНЕ (по составам), а не текущий
    Player.team на момент пересчёта — важно при трансфере в середине сезона."""

    def test_occupant_team_reflects_latest_lineup_not_current_player_team(self):
        player = Player.objects.create(first_name="Транзит", last_name="Игроков", team=self.team_a)

        early_match = self.make_match(
            home=self.team_a, away=self.team_b,
            start_time=timezone.now() - timedelta(days=60),
        )
        self.add_to_lineup(early_match, player, self.team_a, 'home', 'ST')
        self.add_player_aggregate(player, early_match, 8.0)

        # Игрок "перешёл" в team_b — более поздний матч уже за новый клуб.
        late_match = self.make_match(
            home=self.team_b, away=self.team_a,
            start_time=timezone.now() - timedelta(days=5),
        )
        self.add_to_lineup(late_match, player, self.team_b, 'home', 'ST')
        self.add_player_aggregate(player, late_match, 8.0)

        # В справочнике Player.team НЕ обновлён (реалистичный лаг с парсером KFF).
        self.assertEqual(player.team_id, self.team_a.id)

        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="ST")
        self.assertEqual(str(slot.object_id), str(player.id))
        self.assertEqual(slot.occupant_team_name, self.team_b.name)


class RefereeScoreTests(SeasonSquadTestCase):
    """Продуктовое ревью 2026-08-22: рейтинг судьи не должен считаться
    только по decision_quality — влияние на матч (influence_score) и
    воспринимаемая справедливость матча (MatchEvaluation.fairness) тоже
    должны учитываться (см. докстринг _build_referee_pool)."""

    def _rate_referee_match(self, match, decision_quality, influence_score, fairness=None):
        """Строит RefereeMatchAggregate НАПРЯМУЮ (тот же приём, что
        add_player_aggregate) с той же формулой, что и
        aggregates/tasks.py::recalculate_referee_aggregates — фолбэк
        fairness=decision_quality при отсутствии отдельной оценки
        справедливости, см. докстринг recalculate_referee_aggregates."""
        if fairness is None:
            fairness = decision_quality
        performance_score = (
            0.6 * decision_quality + 0.3 * fairness + 0.1 * (10 - influence_score / 10)
        )
        RefereeMatchAggregate.objects.create(
            referee=match.referee, match=match,
            avg_decision_quality=decision_quality, avg_influence=influence_score,
            avg_fairness=fairness, total_votes=1, performance_score=performance_score,
        )

    def test_high_decision_quality_but_high_influence_can_lose_to_fairer_invisible_referee(self):
        """Судья A: высокое качество решений, но ОГРОМНОЕ влияние на матч
        (стал "героем/злодеем" вечера) и никто не оценил справедливость
        отдельно (фолбэк на decision_quality). Судья B: чуть ниже
        decision_quality, зато незаметен (influence=0) и матч сочли
        справедливым (fairness=10). Итоговая формула должна отдать
        предпочтение B — раньше decision_quality-only формула выбрала бы A."""
        referee_a = Referee.objects.create(first_name="Явный", last_name="Герой")
        referee_b = Referee.objects.create(first_name="Незаметный", last_name="Судья")

        for _ in range(2):
            match = self.make_match(referee=referee_a)
            self._rate_referee_match(match, decision_quality=9, influence_score=100)

        for _ in range(2):
            match = self.make_match(referee=referee_b)
            self._rate_referee_match(match, decision_quality=7, influence_score=0, fairness=10)

        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="REFEREE")
        self.assertEqual(str(slot.object_id), str(referee_b.id))

    def test_referee_below_min_matches_is_excluded(self):
        referee = Referee.objects.create(first_name="Один", last_name="Матч")
        match = self.make_match(referee=referee)
        self._rate_referee_match(match, decision_quality=10, influence_score=0, fairness=10)

        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="REFEREE")
        self.assertIsNone(slot.content_type_id)


class CoachConfidenceTests(SeasonSquadTestCase):
    """Тренер тоже подчиняется общему порогу MIN_MATCHES_FOR_CANDIDATE —
    не может попасть в состав по одному оценённому матчу."""

    def test_coach_below_min_matches_is_excluded(self):
        coach = Coach.objects.create(first_name="Один", last_name="Матч", team=self.team_a)
        match = self.make_match()
        CoachMatchAggregate.objects.create(
            coach=coach, match=match,
            avg_tactics=9, avg_substitutions=9, avg_management=9, avg_impact=9, total_votes=5,
        )
        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="COACH")
        self.assertIsNone(slot.content_type_id)

    def test_coach_confidence_flag_reflects_vote_threshold(self):
        coach = Coach.objects.create(first_name="Уверенный", last_name="Тренер", team=self.team_a)
        for _ in range(2):
            match = self.make_match()
            CoachMatchAggregate.objects.create(
                coach=coach, match=match,
                avg_tactics=8, avg_substitutions=8, avg_management=8, avg_impact=8,
                total_votes=CONFIDENT_VOTES_THRESHOLD,
            )
        best_xi = recompute_best_xi(self.season)
        slot = SeasonBestXISlot.objects.get(best_xi=best_xi, slot_code="COACH")
        self.assertEqual(str(slot.object_id), str(coach.id))
        self.assertTrue(slot.is_confident)


class WidgetEmbedTests(TestCase):
    """CSP-регрессия 2026-08-22: @xframe_options_exempt на best_xi_widget
    был, а WIDGET_PATH_PATTERN в dopx/middleware.py — нет, из-за чего
    frame-ancestors 'self' блокировал iframe у партнёра несмотря на
    рабочую ссылку. См. dopx/middleware.py::ContentSecurityPolicyMiddleware."""

    def test_widget_without_season_id_allows_any_frame_ancestor(self):
        response = self.client.get(reverse("season_squad:widget"))
        self.assertEqual(response.status_code, 200)
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors *", csp)

    def test_widget_with_season_id_allows_any_frame_ancestor(self):
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        response = self.client.get(reverse("season_squad:widget", args=[season.id]))
        self.assertEqual(response.status_code, 200)
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors *", csp)

    def test_public_best_xi_page_keeps_strict_frame_ancestors(self):
        """Сам сайт (не embed-виджет) не должен по ошибке стать более
        открытым — только точечные /widget/-пути ослабляют CSP."""
        league = League.objects.create(name="Test League", country="KZ")
        season = Season.objects.create(league=league, year="2026", is_active=True)
        response = self.client.get(reverse("season_squad:best_xi", args=[season.id]))
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors 'self'", csp)

    @override_settings(WIDGET_ALLOWED_ORIGINS=["https://partner.kz", "https://another.kz"])
    def test_widget_respects_configured_allow_list(self):
        """Аудит 2026-09-04: пока WIDGET_ALLOWED_ORIGINS пуст, виджеты
        встраиваемы куда угодно ("*") — это осознанное и временное решение,
        см. dopx/settings.py. Как только список заполнен, middleware должен
        сразу отдавать точный список доменов, без изменений в коде."""
        response = self.client.get(reverse("season_squad:widget"))
        csp = response.headers.get("Content-Security-Policy", "")
        self.assertIn("frame-ancestors https://partner.kz https://another.kz;", csp)
        self.assertNotIn("frame-ancestors *", csp)


@override_settings(CACHES=LOCMEM_CACHE)
class RecomputeLockTests(SeasonSquadTestCase):
    """Redis-lock (продуктовое ревью 2026-08-22): без него Celery Beat и
    ручной триггер могли одновременно пересчитать один и тот же сезон и
    исказить историю рангов. LocMemCache вместо реального Redis — тесты
    не должны зависеть от поднятого Redis-сервера (см. dopx/settings.py::
    CACHES, 'default' в проде — RedisCache)."""

    def setUp(self):
        super().setUp()
        cache.clear()

    def test_second_call_is_skipped_while_lock_held(self):
        lock_key = f"season_squad:recompute:{self.season.id}"
        self.assertTrue(cache.add(lock_key, "1", timeout=300))

        recompute_best_xi_task(str(self.season.id))

        # Задача должна была выйти немедленно по занятому локу — до
        # SeasonBestXI.objects.get_or_create() дело не дошло вообще.
        self.assertFalse(SeasonBestXI.objects.filter(season=self.season).exists())

        cache.delete(lock_key)

    def test_lock_is_released_after_successful_run(self):
        recompute_best_xi_task(str(self.season.id))
        lock_key = f"season_squad:recompute:{self.season.id}"
        self.assertIsNone(cache.get(lock_key))
        self.assertTrue(SeasonBestXI.objects.filter(season=self.season).exists())
