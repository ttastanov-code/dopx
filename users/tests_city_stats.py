# users/tests_city_stats.py
"""Тесты статистики по городам: битва городов, разрез для дашборда, география болельщиков."""
from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from evaluations.models import ContextEvaluation, EvaluationSession
from leagues.models import League
from matches.models import Match
from predictions.models import MatchPrediction
from seasons.models import Season
from teams.models import Team
from users import city_stats
from users.models import User

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(CACHES=LOCMEM_CACHE)
class CityStatsTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.home = Team.objects.create(name="Кайрат")
        self.away = Team.objects.create(name="Астана")
        self._n = 0
        self._m = 0

    def tearDown(self):
        cache.clear()

    def make_user(self, city, verified=True):
        self._n += 1
        return User.objects.create_user(
            username=f"u{self._n}", email=f"u{self._n}@test.local", password="x",
            city=city, is_verified=verified,
        )

    def make_users(self, city, count):
        return [self.make_user(city) for _ in range(count)]

    def make_match(self, home_score=2, away_score=1, days_ago=1):
        self._m += 1
        start = timezone.now() - timedelta(days=days_ago)
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status="finished", start_time=start, voting_open_until=start + timedelta(hours=48),
            home_score=home_score, away_score=away_score, sportmonks_id=str(9000 + self._m),
        )

    def complete_eval(self, user, match, days_ago=1):
        EvaluationSession.objects.create(
            user=user, match=match, status="completed",
            completed_at=timezone.now() - timedelta(days=days_ago),
        )


class CityBattleTests(CityStatsTestCase):
    def test_city_below_min_users_is_hidden(self):
        """Город с числом участников меньше порога не попадает в рейтинг."""
        self.make_users("Алматы", 10)
        self.make_users("Астана", 9)
        cities = [r["city"] for r in city_stats.city_battle("all")]
        self.assertEqual(cities, ["Алматы"])

    def test_ranked_by_evaluations_per_user(self):
        """Маленький активный город обходит большой пассивный."""
        big = self.make_users("Алматы", 20)
        small = self.make_users("Шымкент", 10)
        match = self.make_match()
        for u in big[:10]:
            self.complete_eval(u, match)   # 10 оценок / 20 = 0.5
        for u in small:
            self.complete_eval(u, match)   # 10 / 10 = 1.0

        rows = city_stats.city_battle("all")
        self.assertEqual([r["city"] for r in rows], ["Шымкент", "Алматы"])
        self.assertEqual(rows[0]["per_user"], 1.0)
        self.assertEqual(rows[1]["per_user"], 0.5)
        self.assertEqual(rows[0]["rank"], 1)

    def test_period_excludes_old_evaluations(self):
        users = self.make_users("Алматы", 10)
        old = self.make_match(days_ago=60)
        for u in users:
            self.complete_eval(u, old, days_ago=60)

        month = city_stats.city_battle("month")[0]
        all_time = city_stats.city_battle("all")[0]
        self.assertEqual(month["evaluations"], 0)
        self.assertEqual(all_time["evaluations"], 10)

    def test_prediction_accuracy_needs_20_predictions(self):
        users = self.make_users("Алматы", 10)
        match = self.make_match(home_score=2, away_score=1)  # итог '1'
        for u in users:
            MatchPrediction.objects.create(user=u, match=match, choice="1")
        self.assertIsNone(city_stats.city_battle("all")[0]["accuracy"])

        cache.clear()
        match2 = self.make_match(home_score=0, away_score=0)  # итог 'X'
        for i, u in enumerate(users):
            MatchPrediction.objects.create(user=u, match=match2, choice="X" if i < 5 else "2")
        # 10 верных + 5 верных из 20
        self.assertEqual(city_stats.city_battle("all")[0]["accuracy"], 75)

    def test_unverified_and_no_city_users_ignored(self):
        self.make_users("Алматы", 9)
        self.make_user("Алматы", verified=False)
        self.make_user("")
        self.assertEqual(city_stats.city_battle("all"), [])


class DashboardCityBreakdownTests(CityStatsTestCase):
    def test_breakdown_counts_and_conversion(self):
        almaty = self.make_users("Алматы", 4)
        self.make_user("")
        match = self.make_match()
        self.complete_eval(almaty[0], match)

        data = city_stats.dashboard_city_breakdown(days=14)
        rows = {r["city"]: r for r in data["rows"]}
        self.assertEqual(rows["Алматы"]["users"], 4)
        self.assertEqual(rows["Алматы"]["evaluated_ever"], 1)
        self.assertEqual(rows["Алматы"]["conversion_percent"], 25)
        self.assertEqual(rows["Алматы"]["active_period"], 1)
        self.assertEqual(rows["Не указан"]["users"], 1)
        self.assertEqual(data["no_city_users"], 1)
        self.assertEqual(data["cities_count"], 1)


class TeamFanGeographyTests(CityStatsTestCase):
    def support(self, users, team):
        match = self.make_match()
        for u in users:
            ContextEvaluation.objects.create(user=u, match=match, supported_team=team, watched_type="full")
            EvaluationSession.objects.get_or_create(user=u, match=match, defaults={"status": "completed"})

    def test_hidden_below_min_fans(self):
        self.support(self.make_users("Алматы", 9), self.home)
        self.assertIsNone(city_stats.team_fan_geography(self.home))

    def test_percentages_and_other(self):
        fans = (
            self.make_users("Алматы", 6) + self.make_users("Астана", 2)
            + [self.make_user(c) for c in ["Шымкент", "Караганда", "Актобе", "Тараз"]]
        )
        self.support(fans, self.home)

        geo = city_stats.team_fan_geography(self.home)
        self.assertEqual(geo["total"], 12)
        self.assertEqual(geo["rows"][0], {"city": "Алматы", "fans": 6, "percent": 50})
        self.assertEqual(len(geo["rows"]), city_stats.TEAM_FAN_GEO_TOP)
        self.assertEqual(geo["other_fans"], 1)

    def test_fans_of_other_team_not_counted(self):
        self.support(self.make_users("Алматы", 10), self.away)
        self.assertIsNone(city_stats.team_fan_geography(self.home))


@override_settings(CACHES=LOCMEM_CACHE)
class CityLeaderboardViewTests(CityStatsTestCase):
    def test_page_renders_empty_state(self):
        resp = self.client.get(reverse("users:city_leaderboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Битва городов")

    def test_page_lists_city(self):
        self.make_users("Алматы", 10)
        resp = self.client.get(reverse("users:city_leaderboard") + "?period=all")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Алматы")
