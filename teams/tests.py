# teams/tests.py
"""
Тесты teams/services.py::compute_mood_trend (Индекс настроения клуба, MVP
тренд-бейдж, docs/adr/0029-club-mood-index-mvp.md). TestCase — нужен
реальный TeamMatchAggregate (Meta.ordering читается по match__start_time).
"""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from aggregates.models import RefereeMatchAggregate, TeamMatchAggregate
from leagues.models import League
from matches.models import Match
from referees.models import Referee
from seasons.models import Season
from teams.models import Team
from teams.services import (
    build_mood_chart,
    build_sparkline_points,
    compute_mood_series,
    compute_mood_trend,
    find_season_controversial_matches,
)

LOCMEM_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "test-mood-trend"},
}


@override_settings(CACHES=LOCMEM_CACHES)
class MoodTrendServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")

    def _make_match_agg(self, days_ago, score, total_votes=10):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=days_ago),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        return TeamMatchAggregate.objects.create(
            team=self.team, match=match, total_votes=total_votes, performance_score=score,
        )

    def test_less_than_two_matches_returns_none(self):
        self._make_match_agg(days_ago=1, score=7.0)
        self.assertIsNone(compute_mood_trend(self.team))

    def test_rising_trend(self):
        self._make_match_agg(days_ago=5, score=5.0)
        self._make_match_agg(days_ago=4, score=5.0)
        self._make_match_agg(days_ago=3, score=5.0)
        self._make_match_agg(days_ago=2, score=8.0)
        self._make_match_agg(days_ago=1, score=8.0)
        result = compute_mood_trend(self.team)
        self.assertEqual(result["direction"], "up")

    def test_falling_trend(self):
        self._make_match_agg(days_ago=5, score=8.0)
        self._make_match_agg(days_ago=4, score=8.0)
        self._make_match_agg(days_ago=3, score=8.0)
        self._make_match_agg(days_ago=2, score=5.0)
        self._make_match_agg(days_ago=1, score=5.0)
        result = compute_mood_trend(self.team)
        self.assertEqual(result["direction"], "down")

    def test_flat_trend_within_noise_threshold(self):
        self._make_match_agg(days_ago=3, score=7.0)
        self._make_match_agg(days_ago=2, score=7.1)
        self._make_match_agg(days_ago=1, score=7.0)
        result = compute_mood_trend(self.team)
        self.assertEqual(result["direction"], "flat")

    def test_zero_vote_aggregates_excluded(self):
        self._make_match_agg(days_ago=2, score=9.0, total_votes=0)
        self._make_match_agg(days_ago=1, score=5.0)
        # Только 1 строка с total_votes > 0 — недостаточно для тренда.
        self.assertIsNone(compute_mood_trend(self.team))

    def test_result_is_cached(self):
        self._make_match_agg(days_ago=2, score=5.0)
        self._make_match_agg(days_ago=1, score=8.0)
        first = compute_mood_trend(self.team)
        # Добавляем ещё одну (более свежую) строку — если бы результат не
        # кэшировался, второй вызов увидел бы её и мог дать другой ответ.
        self._make_match_agg(days_ago=0, score=1.0)
        second = compute_mood_trend(self.team)
        self.assertEqual(first, second)


@override_settings(CACHES=LOCMEM_CACHES)
class MoodSeriesServiceTests(TestCase):
    """docs/adr/0034-club-mood-index-v2.md — time series (не один бейдж)."""

    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")

    def _make_match_agg(self, days_ago, mood_score, trust_score, total_votes=10):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=days_ago),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        TeamMatchAggregate.objects.create(
            team=self.team, match=match, total_votes=total_votes,
            performance_score=mood_score, own_fans_avg=trust_score,
        )
        return match

    def test_no_matches_returns_empty_list(self):
        self.assertEqual(compute_mood_series(self.team), [])

    def test_points_chronological_with_trust_and_mood(self):
        self._make_match_agg(days_ago=3, mood_score=6.0, trust_score=7.0)
        self._make_match_agg(days_ago=1, mood_score=8.0, trust_score=9.0)
        series = compute_mood_series(self.team)
        self.assertEqual(len(series), 2)
        # Хронологический порядок — старый матч первым (график слева-направо).
        self.assertEqual(series[0]["mood"], 6.0)
        self.assertEqual(series[1]["mood"], 8.0)
        self.assertEqual(series[0]["trust"], 7.0)

    def test_expectation_none_without_predictions(self):
        self._make_match_agg(days_ago=1, mood_score=7.0, trust_score=7.0)
        series = compute_mood_series(self.team)
        self.assertIsNone(series[0]["expectation_pct"])
        self.assertEqual(series[0]["total_predictions"], 0)

    def test_expectation_percentage_from_predictions(self):
        from predictions.models import MatchPrediction
        from django.contrib.auth import get_user_model

        User = get_user_model()
        match = self._make_match_agg(days_ago=1, mood_score=7.0, trust_score=7.0)
        for i in range(3):
            user = User.objects.create_user(username=f"u{i}", email=f"u{i}@example.com", password="pass12345")
            MatchPrediction.objects.create(match=match, user=user, choice=MatchPrediction.CHOICE_HOME)
        user_away = User.objects.create_user(username="u_away", email="u_away@example.com", password="pass12345")
        MatchPrediction.objects.create(match=match, user=user_away, choice=MatchPrediction.CHOICE_AWAY)

        series = compute_mood_series(self.team)
        # self.team — домашняя команда (match.home_team=self.team), 3 из 4
        # прогнозов — на её победу (CHOICE_HOME) => 75%.
        self.assertEqual(series[0]["expectation_pct"], 75.0)
        self.assertEqual(series[0]["total_predictions"], 4)


class BuildSparklinePointsTests(SimpleTestCase):
    def test_fewer_than_two_points_returns_empty(self):
        self.assertEqual(build_sparkline_points([{"v": 5.0}], "v"), "")
        self.assertEqual(build_sparkline_points([], "v"), "")

    def test_all_none_returns_empty(self):
        series = [{"v": None}, {"v": None}]
        self.assertEqual(build_sparkline_points(series, "v"), "")

    def test_two_valid_points_returns_coordinates(self):
        series = [{"v": 0.0}, {"v": 10.0}]
        points = build_sparkline_points(series, "v", value_max=10.0, width=100, height=100, pad_x=10, pad_y=10)
        self.assertNotEqual(points, "")
        coords = [tuple(map(float, p.split(","))) for p in points.split(" ")]
        self.assertEqual(len(coords), 2)
        # value=0 -> внизу (y большой), value=value_max -> вверху (y малый).
        self.assertGreater(coords[0][1], coords[1][1])

    def test_gap_in_series_skipped_without_breaking_positions(self):
        series = [{"v": 5.0}, {"v": None}, {"v": 8.0}]
        points = build_sparkline_points(series, "v", value_max=10.0)
        coords = [tuple(map(float, p.split(","))) for p in points.split(" ")]
        self.assertEqual(len(coords), 2)  # средняя точка (None) пропущена


class BuildMoodChartTests(SimpleTestCase):
    """teams/services.py::build_mood_chart — премиальный редизайн графика
    "Индекс настроения клуба", ПЕРЕСОБРАН ВТОРОЙ РАЗ 2026-09-07 (живой скрин:
    "10" рисовалось битым текстом, бары "ожиданий" были невидимы, потом —
    один закрашенный бар на фоне девяти пустых заглушек). Тесты обновлены
    под ТЕКУЩИЙ контракт build_mood_chart:
      · `mood`/`trust` — ВСЕГДА dict (никогда `None`) при непустом `series`,
        внутри свой флаг `has_data` (линию рисовать или нет) и своя
        `gridlines` — mood/trust больше не делят один список сетки на весь
        chart, у каждого своя (см. докстринг _build_series_chart).
      · `expectation` — dict с `bars` (список, включая записи `has_data=False`
        для матчей без прогнозов — их не исключают, а помечают).
      · `has_expectation_data` — требует МИНИМАЛЬНОГО покрытия (не любое
        ненулевое количество), иначе секция с одним баром на фоне девяти
        пустых читалась как "почти всё сломано" (EXPECTATION_MIN_COVERAGE_*).
      · `latest_mood`/`latest_trust` — последнее РЕАЛЬНО известное значение
        (сканирование с конца), а не `series[-1][key]` без проверки на
        `None` — та самая причина пустого "Доверие сейчас: /10" на скрине."""

    def _point(self, label="01.09", opponent="Соперник", mood=6.0, trust=None, expectation_pct=None):
        return {"label": label, "opponent": opponent, "mood": mood, "trust": trust, "expectation_pct": expectation_pct}

    def test_empty_series_returns_none(self):
        self.assertIsNone(build_mood_chart([]))

    def test_missing_trust_data_has_data_false_but_dict_present(self):
        series = [self._point(mood=6.0), self._point(mood=7.0)]
        chart = build_mood_chart(series)
        self.assertTrue(chart["mood"]["has_data"])
        self.assertIsNotNone(chart["trust"])  # dict, не None — шаблон читает .has_data
        self.assertFalse(chart["trust"]["has_data"])

    def test_single_trust_point_insufficient_for_line(self):
        # Один валидный trust на фоне двух mood — линию из одной точки не
        # построить (тот же порог "меньше 2 точек", что у build_sparkline_points).
        series = [self._point(mood=6.0, trust=5.0), self._point(mood=7.0, trust=None)]
        chart = build_mood_chart(series)
        self.assertFalse(chart["trust"]["has_data"])
        self.assertEqual(chart["latest_trust"], 5.0)  # число всё равно доступно

    def test_both_series_present_with_two_plus_points(self):
        series = [self._point(mood=6.0, trust=5.0), self._point(mood=7.0, trust=6.0)]
        chart = build_mood_chart(series)
        self.assertTrue(chart["mood"]["has_data"])
        self.assertTrue(chart["trust"]["has_data"])
        self.assertEqual(len(chart["mood"]["dots"]), 2)
        self.assertEqual(len(chart["trust"]["dots"]), 2)

    def test_mood_gridlines_are_whole_numbers_only(self):
        # Только 0/5/10 — БЕЗ дробных 2.5/7.5 (см. докстринг модуля,
        # пункт 2 — дробная точка была на грани обрезания при масштабировании).
        series = [self._point(mood=6.0), self._point(mood=7.0)]
        chart = build_mood_chart(series)
        labels = [g["label"] for g in chart["mood"]["gridlines"]]
        self.assertEqual(labels, ["0", "5", "10"])

    def test_trust_gridlines_are_minimal(self):
        series = [self._point(mood=6.0, trust=5.0), self._point(mood=7.0, trust=6.0)]
        chart = build_mood_chart(series)
        labels = [g["label"] for g in chart["trust"]["gridlines"]]
        self.assertEqual(labels, ["0", "10"])

    def test_expectation_bars_include_no_data_entries_not_excluded(self):
        # pct=None не выкидывается из bars — попадает как has_data=False, шаблон
        # рисует ему пунктирную заглушку вместо провала в ряду.
        series = [self._point(mood=6.0, expectation_pct=40.0), self._point(mood=7.0, expectation_pct=None)]
        chart = build_mood_chart(series)
        bars = chart["expectation"]["bars"]
        self.assertEqual(len(bars), 2)
        self.assertTrue(bars[0]["has_data"])
        self.assertEqual(bars[0]["pct"], 40.0)
        self.assertFalse(bars[1]["has_data"])

    def test_has_expectation_data_false_when_all_none(self):
        series = [self._point(mood=6.0), self._point(mood=7.0)]
        chart = build_mood_chart(series)
        self.assertFalse(chart["has_expectation_data"])

    def test_has_expectation_data_true_even_with_single_data_point(self):
        # ИСТОРИЯ (2026-09-07): сначала секцию прятали при низком покрытии
        # (порог "минимум 3 точки и 30%"), но продуктовый фидбек был обратный
        # — "а где эти проценты?". Порог вернули к "показываем, если есть
        # хоть одна точка" (см. докстринг EXPECTATION_MIN_COVERAGE_* в
        # teams/services.py) — 1 бар из 10 с приглушёнными прочерками рядом
        # уже не выглядит поломкой теперь, когда его цвет (--color-secondary)
        # не совпадает с --color-success графика "Доверие".
        series = [self._point(mood=6.0, expectation_pct=50.0)] + [self._point(mood=6.0) for _ in range(9)]
        chart = build_mood_chart(series)
        self.assertTrue(chart["has_expectation_data"])
        bars = chart["expectation"]["bars"]
        self.assertTrue(bars[0]["has_data"])
        self.assertTrue(all(not b["has_data"] for b in bars[1:]))

    def test_latest_uses_last_non_null_value_not_series_tail(self):
        # Ровно баг со скриншота: у самого свежего матча trust отсутствует, у
        # более раннего — есть. latest_trust должен взять более раннее
        # значение, а не показать пустоту при формально непустом chart["trust"].
        series = [
            self._point(label="01.09", opponent="A", mood=6.0, trust=5.0),
            self._point(label="08.09", opponent="B", mood=7.5, trust=None),
        ]
        chart = build_mood_chart(series)
        self.assertEqual(chart["latest_mood"], 7.5)
        self.assertEqual(chart["latest_trust"], 5.0)

    def test_latest_values_and_labels(self):
        series = [self._point(label="01.09", opponent="A", mood=6.0, trust=5.0),
                   self._point(label="08.09", opponent="B", mood=7.5, trust=6.5)]
        chart = build_mood_chart(series)
        self.assertEqual(chart["latest_mood"], 7.5)
        self.assertEqual(chart["latest_trust"], 6.5)
        self.assertEqual(chart["oldest_label"], "A 01.09")
        self.assertEqual(chart["newest_label"], "B 08.09")


@override_settings(CACHES=LOCMEM_CACHES)
class SeasonControversialMatchesTests(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026")
        self.team = Team.objects.create(name="Team")
        self.opponent = Team.objects.create(name="Opponent")
        self.referee = Referee.objects.create(first_name="Судья", last_name="Судьич")

    def _make_match_with_gap(self, home_avg, away_avg):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished", home_score=1, away_score=0,
        )
        RefereeMatchAggregate.objects.create(
            referee=self.referee, match=match, total_votes=10,
            home_fans_avg=home_avg, away_fans_avg=away_avg,
        )
        return match

    def test_small_gap_excluded(self):
        self._make_match_with_gap(7.0, 6.5)
        self.assertEqual(find_season_controversial_matches(self.team, self.season), [])

    def test_large_gap_included_and_sorted(self):
        self._make_match_with_gap(8.5, 4.0)  # gap 4.5
        self._make_match_with_gap(7.0, 5.0)  # gap 2.0
        result = find_season_controversial_matches(self.team, self.season)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["gap"], 4.5)
        self.assertEqual(result[1]["gap"], 2.0)

    def test_missing_segment_excluded(self):
        match = Match.objects.create(
            league=self.league, season=self.season,
            home_team=self.team, away_team=self.opponent,
            start_time=timezone.now() - timedelta(days=1),
            voting_open_until=timezone.now() + timedelta(days=1),
            status="finished",
        )
        RefereeMatchAggregate.objects.create(
            referee=self.referee, match=match, total_votes=10,
            home_fans_avg=8.0, away_fans_avg=None,
        )
        self.assertEqual(find_season_controversial_matches(self.team, self.season), [])
