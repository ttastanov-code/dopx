# matches/tests.py
"""
Тесты matches/services.py::build_match_dna ("ДНК матча", фаза 1,
docs/adr/0028-match-dna-phase1.md). Все функции модуля читают только
переданные объекты — build_match_dna сам по себе не делает запросов к БД
(это забота вызывающей стороны, MatchDetailView) — SimpleTestCase с
SimpleNamespace вместо реальных Django-моделей, БД не нужна.
"""
from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from matches.services import (
    _consensus_level,
    _describe_controversial_episode,
    _describe_hero,
    _describe_momentum,
    _describe_referee_divergence,
    _describe_turning_point,
    _drama_level,
    build_match_dna,
)


def _event(minute, event_type="goal", player=None):
    return SimpleNamespace(
        minute=minute, event_type=event_type, display_minute=str(minute),
        player=player, player_id=(player.id if player else None),
    )


def _evaluation(entertainment, tension, fairness):
    return SimpleNamespace(entertainment=entertainment, tension=tension, fairness=fairness)


class DramaLevelTests(SimpleTestCase):
    def test_high(self):
        self.assertEqual(_drama_level(60.0), "high")
        self.assertEqual(_drama_level(80.0), "high")

    def test_medium(self):
        self.assertEqual(_drama_level(30.0), "medium")
        self.assertEqual(_drama_level(59.9), "medium")

    def test_low(self):
        self.assertEqual(_drama_level(0.0), "low")
        self.assertEqual(_drama_level(29.9), "low")


class DescribeMomentumTests(SimpleTestCase):
    def test_no_events_returns_empty(self):
        self.assertEqual(_describe_momentum([]), [])

    def test_single_event_in_window_not_a_momentum_point(self):
        """Одно событие в окне — не "момент", просто строка таймлайна."""
        self.assertEqual(_describe_momentum([_event(10)]), [])

    def test_two_goals_same_window_described_as_goals(self):
        events = [_event(78, "goal"), _event(82, "goal")]
        points = _describe_momentum(events)
        self.assertEqual(len(points), 1)
        self.assertIn("гол", points[0])
        self.assertIn("75", points[0])

    def test_non_goal_cluster_described_generically(self):
        events = [_event(10, "yellow_card"), _event(12, "yellow_card")]
        points = _describe_momentum(events)
        self.assertEqual(len(points), 1)
        self.assertIn("событий", points[0])

    def test_returns_at_most_two_points(self):
        events = [
            _event(5), _event(7),      # окно 0-15
            _event(20), _event(22),    # окно 15-30
            _event(80), _event(82),    # окно 75-90
        ]
        self.assertLessEqual(len(_describe_momentum(events)), 2)


class DescribeRefereeDivergenceTests(SimpleTestCase):
    def _match(self):
        return SimpleNamespace(
            home_team=SimpleNamespace(name="Кайрат"),
            away_team=SimpleNamespace(name="Актобе"),
        )

    def test_none_aggregate_returns_empty(self):
        self.assertEqual(_describe_referee_divergence(self._match(), None), "")

    def test_missing_segment_returns_empty(self):
        agg = SimpleNamespace(home_fans_avg=7.0, away_fans_avg=None)
        self.assertEqual(_describe_referee_divergence(self._match(), agg), "")

    def test_small_gap_returns_empty(self):
        agg = SimpleNamespace(home_fans_avg=7.0, away_fans_avg=6.0)
        self.assertEqual(_describe_referee_divergence(self._match(), agg), "")

    def test_large_gap_returns_sentence(self):
        agg = SimpleNamespace(home_fans_avg=8.5, away_fans_avg=4.0)
        text = _describe_referee_divergence(self._match(), agg)
        self.assertIn("Кайрат", text)
        self.assertIn("Актобе", text)
        self.assertIn("4.5", text)


class BuildMatchDnaTests(SimpleTestCase):
    def test_none_aggregate_returns_none(self):
        self.assertIsNone(build_match_dna(SimpleNamespace(), None, []))

    def test_zero_votes_returns_none(self):
        agg = SimpleNamespace(total_votes=0, drama_index=50.0, turning_point_ratio=0.0)
        self.assertIsNone(build_match_dna(SimpleNamespace(), agg, []))

    def test_with_votes_returns_dict(self):
        match = SimpleNamespace(
            home_team=SimpleNamespace(name="Кайрат"), away_team=SimpleNamespace(name="Актобе"),
        )
        agg = SimpleNamespace(total_votes=10, drama_index=65.0, turning_point_ratio=0.0)
        result = build_match_dna(match, agg, [_event(78), _event(80)])
        self.assertEqual(result["drama_level"], "high")
        self.assertEqual(result["drama_index"], 65.0)
        self.assertIsInstance(result["momentum_points"], list)
        self.assertEqual(result["referee_divergence"], "")
        # Фаза 2 (docs/adr/0033) — новые ключи присутствуют, даже когда
        # соответствующего сигнала нет (не ломаем контракт словаря).
        self.assertIsNone(result["hero"])
        self.assertEqual(result["turning_point_text"], "")
        self.assertIsNone(result["consensus_level"])
        self.assertEqual(result["controversial_episode"], "")


class DescribeHeroTests(SimpleTestCase):
    def test_empty_list_returns_none(self):
        self.assertIsNone(_describe_hero([]))

    def test_returns_first_player_with_score(self):
        hero_agg = SimpleNamespace(player=SimpleNamespace(id=1, first_name="A"), performance_score=8.7)
        result = _describe_hero([hero_agg, SimpleNamespace(player=SimpleNamespace(id=2), performance_score=6.0)])
        self.assertEqual(result["score"], 8.7)
        self.assertIs(result["player"], hero_agg.player)


class DescribeTurningPointTests(SimpleTestCase):
    def test_below_threshold_returns_empty(self):
        agg = SimpleNamespace(turning_point_ratio=0.1)
        self.assertEqual(_describe_turning_point(agg), "")

    def test_at_or_above_threshold_returns_sentence(self):
        agg = SimpleNamespace(turning_point_ratio=0.5)
        text = _describe_turning_point(agg)
        self.assertIn("50%", text)
        self.assertIn("переломный момент", text)


class ConsensusLevelTests(SimpleTestCase):
    def test_fewer_than_two_evaluations_returns_none(self):
        self.assertIsNone(_consensus_level([_evaluation(8, 8, 8)]))

    def test_identical_scores_high_consensus(self):
        evals = [_evaluation(8, 8, 8) for _ in range(5)]
        self.assertEqual(_consensus_level(evals), "high")

    def test_wildly_different_scores_low_consensus(self):
        evals = [_evaluation(10, 10, 10), _evaluation(1, 1, 1), _evaluation(10, 1, 5)]
        self.assertEqual(_consensus_level(evals), "low")

    def test_moderate_spread_medium_consensus(self):
        # Композиты 8/5/6 -> population stdev ~1.25 — строго между
        # CONSENSUS_HIGH_STDEV=1.0 и CONSENSUS_LOW_STDEV=2.5.
        evals = [_evaluation(8, 8, 8), _evaluation(5, 5, 5), _evaluation(6, 6, 6)]
        self.assertEqual(_consensus_level(evals), "medium")


class DescribeControversialEpisodeTests(SimpleTestCase):
    def test_disallowed_goal_takes_priority(self):
        scorer = SimpleNamespace(id=1)
        events = [_event(60, "disallowed_goal", player=scorer), _event(70, "red_card", player=scorer)]
        text = _describe_controversial_episode(events, None)
        self.assertIn("Отменённый гол", text)
        self.assertIn("60", text)

    def test_red_card_only_shown_with_referee_divergence(self):
        events = [_event(45, "red_card", player=SimpleNamespace(id=2))]
        low_divergence_agg = SimpleNamespace(home_fans_avg=7.0, away_fans_avg=6.5)
        self.assertEqual(_describe_controversial_episode(events, low_divergence_agg), "")

        high_divergence_agg = SimpleNamespace(home_fans_avg=8.5, away_fans_avg=4.0)
        text = _describe_controversial_episode(events, high_divergence_agg)
        self.assertIn("Красная карточка", text)
        self.assertIn("45", text)

    def test_no_signal_returns_empty(self):
        events = [_event(10, "goal"), _event(50, "yellow_card")]
        self.assertEqual(_describe_controversial_episode(events, None), "")
