# matches/tests.py
"""Тесты build_match_dna (без БД, на SimpleNamespace)."""
from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from matches.services import (
    _consensus_level,
    _describe_antihero,
    _describe_consensus_text,
    _describe_controversial_episode,
    _describe_fan_mood,
    _describe_hero,
    _describe_momentum,
    _match_timeline,
    _describe_archetype,
    _describe_expectations,
    _describe_xg,
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
        """Одно событие в окне — не «момент»."""
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
        self.assertIn("2 события", points[0])

    def test_returns_at_most_two_points(self):
        events = [
            _event(5), _event(7),  # окно 0-15
            _event(20), _event(22),  # окно 15-30
            _event(80), _event(82),  # окно 75-90
        ]
        self.assertLessEqual(len(_describe_momentum(events)), 2)


class MatchTimelineTests(SimpleTestCase):
    def _ev(self, minute, event_type="yellow_card", side="home"):
        e = _event(minute, event_type)
        e.team_side = side
        return e

    def test_empty_events_no_timeline(self):
        self.assertEqual(_match_timeline([]), [])

    def test_split_by_team_goals_and_hot(self):
        events = [
            self._ev(5), self._ev(78, "goal"), self._ev(80, side="away"), self._ev(92, "goal", side="away"),
        ]
        timeline = _match_timeline(events)

        self.assertEqual(len(timeline), 6)
        last = timeline[5]  # 92' — в последнем отрезке
        self.assertEqual((last["home"], last["away"]), (1, 2))
        self.assertEqual((last["home_goals"], last["away_goals"]), (1, 1))
        self.assertEqual(last["goals"], 2)
        self.assertTrue(last["hot"])
        self.assertEqual(last["away_height"], 100)
        self.assertEqual(last["home_height"], 50)
        self.assertEqual(timeline[1]["events"], 0)

    def test_extra_time_adds_windows(self):
        self.assertEqual(len(_match_timeline([_event(110)])), 8)


class DnaInsightsTests(SimpleTestCase):
    def _match(self, home_score, away_score):
        result = "1" if home_score > away_score else ("2" if home_score < away_score else "X")
        return SimpleNamespace(
            home_team=SimpleNamespace(name="Кайрат"), away_team=SimpleNamespace(name="Актобе"),
            home_score=home_score, away_score=away_score, final_result=result,
        )

    def _counts(self, home_pct, draw_pct, away_pct, total=20):
        return {"total": total, "home_pct": home_pct, "draw_pct": draw_pct, "away_pct": away_pct}

    def test_xg_unfair_score(self):
        xg = _describe_xg(self._match(0, 1), SimpleNamespace(xg=2.1), SimpleNamespace(xg=0.4))
        self.assertEqual(xg["verdict"], "unfair")
        self.assertIn("Кайрат", xg["text"])

    def test_xg_deserved_and_missing(self):
        self.assertEqual(_describe_xg(self._match(2, 0), SimpleNamespace(xg=1.9), SimpleNamespace(xg=0.5))["verdict"], "deserved")
        self.assertIsNone(_describe_xg(self._match(2, 0), SimpleNamespace(xg=None), SimpleNamespace(xg=0.5)))

    def test_expectations_and_sensation_archetype(self):
        match = self._match(0, 1)
        exp = _describe_expectations(match, self._counts(70, 20, 10), None)
        self.assertEqual(exp["guessed_pct"], 10)
        self.assertEqual(exp["favorite_label"], "Кайрат")
        self.assertEqual(exp["sensation"], 70)
        self.assertEqual(_describe_archetype(match, "medium", exp, None, None)["title"], "Сенсация")

    def test_expectations_need_min_predictions(self):
        self.assertIsNone(_describe_expectations(self._match(1, 0), self._counts(50, 25, 25, total=2), None))

    def test_archetype_thriller_and_rout(self):
        self.assertEqual(_describe_archetype(self._match(3, 2), "high", None, None, None)["title"], "Триллер до конца")
        self.assertEqual(_describe_archetype(self._match(4, 0), "medium", None, None, None)["title"], "Разгром")


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
        # Ключи фазы 2 есть и без данных.
        self.assertIsNone(result["hero"])
        self.assertEqual(result["turning_point_text"], "")
        self.assertIsNone(result["consensus_level"])
        self.assertEqual(result["controversial_episode"], "")
        # Ключи фазы 3 есть и без данных.
        self.assertIsNone(result["antihero"])
        self.assertEqual(result["consensus_text"], "")
        self.assertEqual(result["fan_mood_text"], "")


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
        # stdev ~1.25 — между порогами консенсуса.
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


class DescribeAntiheroTests(SimpleTestCase):
    """Антигерой и настроение фанатов."""

    def _agg(self, player_id, score):
        return SimpleNamespace(player=SimpleNamespace(id=player_id), player_id=player_id, performance_score=score)

    def test_empty_worst_players_returns_none(self):
        self.assertIsNone(_describe_antihero([self._agg(1, 8.0)], []))

    def test_returns_worst_player_with_score(self):
        hero_agg = self._agg(1, 8.7)
        worst_agg = self._agg(2, 3.2)
        result = _describe_antihero([hero_agg], [worst_agg])
        self.assertEqual(result["score"], 3.2)
        self.assertIs(result["player"], worst_agg.player)

    def test_same_player_as_hero_returns_none(self):
        """Единственный игрок — антигероя не показываем."""
        only_agg = self._agg(1, 6.0)
        self.assertIsNone(_describe_antihero([only_agg], [only_agg]))

    def test_no_hero_still_returns_antihero(self):
        """Антигерой не зависит от героя."""
        worst_agg = self._agg(2, 2.0)
        result = _describe_antihero([], [worst_agg])
        self.assertEqual(result["score"], 2.0)


class DescribeFanMoodTests(SimpleTestCase):
    def _agg(self, entertainment):
        return SimpleNamespace(avg_entertainment=entertainment)

    def test_empty_fan_support_returns_empty(self):
        self.assertEqual(_describe_fan_mood(self._agg(8.0), []), "")

    def test_below_min_votes_returns_empty(self):
        fan_support = [{"supported_team__name": "Кайрат", "count": 2}]
        self.assertEqual(_describe_fan_mood(self._agg(8.0), fan_support), "")

    def test_at_min_votes_returns_sentence_with_percent_and_entertainment(self):
        fan_support = [
            {"supported_team__name": "Кайрат", "count": 8},
            {"supported_team__name": "Актобе", "count": 2},
        ]
        text = _describe_fan_mood(self._agg(7.5), fan_support)
        self.assertIn("7.5", text)
        self.assertIn("80%", text)
        self.assertIn("Кайрат", text)


class DescribeConsensusTextTests(SimpleTestCase):
    def test_high(self):
        self.assertIn("единодушны", _describe_consensus_text("high"))

    def test_low(self):
        self.assertIn("разошлись сильно", _describe_consensus_text("low"))

    def test_medium(self):
        self.assertIn("умеренно", _describe_consensus_text("medium"))

    def test_none_returns_empty(self):
        self.assertEqual(_describe_consensus_text(None), "")


class BuildMatchDnaPhase3Tests(SimpleTestCase):
    """build_match_dna прокидывает worst_players/fan_support."""

    def _match(self):
        return SimpleNamespace(home_team=SimpleNamespace(name="Кайрат"), away_team=SimpleNamespace(name="Актобе"))

    def _agg(self, player_id, score):
        return SimpleNamespace(player=SimpleNamespace(id=player_id), player_id=player_id, performance_score=score)

    def test_antihero_and_fan_mood_present_when_data_given(self):
        agg = SimpleNamespace(total_votes=10, drama_index=65.0, turning_point_ratio=0.0, avg_entertainment=7.0)
        hero_agg = self._agg(1, 8.7)
        worst_agg = self._agg(2, 3.2)
        fan_support = [
            {"supported_team__name": "Кайрат", "count": 8},
            {"supported_team__name": "Актобе", "count": 2},
        ]
        result = build_match_dna(
            self._match(), agg, [],
            top_players=[hero_agg], worst_players=[worst_agg], fan_support=fan_support,
        )
        self.assertEqual(result["antihero"]["score"], 3.2)
        self.assertIn("Кайрат", result["fan_mood_text"])
        self.assertIn("80%", result["fan_mood_text"])
