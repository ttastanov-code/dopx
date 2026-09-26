# core/tests.py
"""Тесты core: is_rate_limited, is_synthetic_test_email, тексты бейджа надёжности.
CACHES -> LocMemCache.
"""
from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from aggregates.models import PlayerMatchAggregate
from aggregates.services import MIN_VOTES_FOR_DISPLAY
from core.templatetags.rating_extras import bias_segment_text, confidence_badge, stability_label
from core.utils import is_rate_limited, is_synthetic_test_email
from leagues.models import League
from matches.models import Match
from players.models import Player
from seasons.models import Season
from teams.models import Team

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-rate-limiter",
    }
}


class IsSyntheticTestEmailTests(SimpleTestCase):
    """is_synthetic_test_email — исключение ботов и нагрузочных аккаунтов из рассылок."""

    def test_bot_pool_email_is_synthetic(self):
        self.assertTrue(is_synthetic_test_email("test_user_bot_0001@test.dopx.local"))

    def test_load_test_email_is_synthetic(self):
        self.assertTrue(is_synthetic_test_email("loadtest_0042@loadtest.dopx.local"))

    def test_bare_reserved_domain_is_synthetic(self):
        self.assertTrue(is_synthetic_test_email("someone@dopx.local"))

    def test_real_user_email_is_not_synthetic(self):
        self.assertFalse(is_synthetic_test_email("timur@gmail.com"))

    def test_similar_but_different_domain_is_not_synthetic(self):
        """Суффикс не должен совпадать с «dopx.local» в середине домена."""
        self.assertFalse(is_synthetic_test_email("someone@notdopx.local"))
        self.assertFalse(is_synthetic_test_email("someone@dopx.local.evil.com"))

    def test_case_insensitive(self):
        self.assertTrue(is_synthetic_test_email("Test_User_Bot_0001@TEST.DOPX.LOCAL"))

    def test_empty_or_none_is_not_synthetic(self):
        self.assertFalse(is_synthetic_test_email(""))
        self.assertFalse(is_synthetic_test_email(None))

    def test_no_at_sign_is_not_synthetic(self):
        self.assertFalse(is_synthetic_test_email("not-an-email"))


@override_settings(CACHES=LOCMEM_CACHES)
class IsRateLimitedTests(SimpleTestCase):
    def setUp(self):
        # Чистим кэш между тестами.
        from django.core.cache import cache
        cache.clear()

    def test_first_call_is_not_limited(self):
        self.assertFalse(is_rate_limited("k1", limit=3, window_seconds=60))

    def test_stays_under_limit_within_window(self):
        for _ in range(3):
            self.assertFalse(is_rate_limited("k2", limit=3, window_seconds=60))

    def test_exceeds_limit_on_the_next_call(self):
        for _ in range(3):
            is_rate_limited("k3", limit=3, window_seconds=60)
        # 4-й вызов в окне — лимит исчерпан.
        self.assertTrue(is_rate_limited("k3", limit=3, window_seconds=60))

    def test_different_keys_have_independent_buckets(self):
        for _ in range(3):
            is_rate_limited("bucket_a", limit=3, window_seconds=60)
        # Другой ключ — свой бакет.
        self.assertFalse(is_rate_limited("bucket_b", limit=3, window_seconds=60))

    def test_resets_after_window_expires(self):
        for _ in range(2):
            is_rate_limited("k4", limit=2, window_seconds=1)
        self.assertTrue(is_rate_limited("k4", limit=2, window_seconds=1))
        time.sleep(1.1)
        self.assertFalse(is_rate_limited("k4", limit=2, window_seconds=1), "окно истекло — бакет должен обнулиться")

    def test_limit_of_one_blocks_second_call_immediately(self):
        self.assertFalse(is_rate_limited("k5", limit=1, window_seconds=60))
        self.assertTrue(is_rate_limited("k5", limit=1, window_seconds=60))


class BiasSegmentTextTests(SimpleTestCase):
    """bias_segment_text: «свои болельщики» — болельщики команды сущности."""

    def _agg(self, own=None, rival=None, neutral=None):
        return SimpleNamespace(own_fans_avg=own, rival_fans_avg=rival, neutral_avg=neutral)

    def test_no_generic_player_wording_used(self):
        """Фразы «фанаты игрока» быть не должно."""
        text = bias_segment_text(self._agg(own=8.0, rival=7.0, neutral=7.0))
        self.assertNotIn("фанаты игрока", text)
        self.assertIn("свои болельщики", text)
        self.assertIn("болельщики соперника", text)
        self.assertIn("нейтральные зрители", text)

    def test_all_three_segments_present(self):
        text = bias_segment_text(self._agg(own=8.0, rival=7.0, neutral=7.0))
        self.assertIn("8.0", text)
        self.assertIn("7.0", text)

    def test_fewer_than_two_segments_returns_empty(self):
        """Один сегмент — пустая строка."""
        self.assertEqual(bias_segment_text(self._agg(own=8.0)), "")
        self.assertEqual(bias_segment_text(None), "")

    def test_two_segments_is_enough(self):
        text = bias_segment_text(self._agg(own=8.0, rival=7.0))
        self.assertTrue(text)


class StabilityLabelTests(SimpleTestCase):
    def test_label_does_not_repeat_word_mneniya(self):
        """stability_label возвращает только прилагательное."""
        self.assertNotIn("мнения", stability_label(0.3))
        self.assertNotIn("мнения", stability_label(0.7))
        self.assertNotIn("мнения", stability_label(1.5))

    def test_high_stability_label(self):
        self.assertEqual(stability_label(1.5), "сходятся")

    def test_low_stability_label(self):
        self.assertEqual(stability_label(0.7), "расходятся")

    def test_very_low_stability_label(self):
        self.assertEqual(stability_label(0.3), "расходятся сильно")

    def test_invalid_value_returns_empty(self):
        self.assertEqual(stability_label(None), "")
        self.assertEqual(stability_label("n/a"), "")


class ConfidenceBadgeTooltipTests(SimpleTestCase):
    """confidence_badge: разброс и лагеря одним предложением.
    databases = {"default"} — get_setting ходит в БД.
    """

    databases = {"default"}

    def _agg(self, total_votes=5, stability_index=0.7, own=8.0, rival=7.0, neutral=7.0):
        return SimpleNamespace(
            total_votes=total_votes, stability_index=stability_index,
            own_fans_avg=own, rival_fans_avg=rival, neutral_avg=neutral,
        )

    def test_tooltip_merges_stability_and_segments_into_one_sentence(self):
        result = confidence_badge(self._agg())
        tooltip = result["tooltip_text"]
        self.assertIn("Мнения расходятся: свои болельщики — 8.0", tooltip)
        self.assertNotIn("фанаты игрока", tooltip)
        # Без задвоения «мнения».
        self.assertNotIn("мнения мнения", tooltip.lower())

    def test_none_aggregate_hides_badge(self):
        self.assertEqual(confidence_badge(None), {"show": False})


class ConfidenceBadgeSampleSizeTests(SimpleTestCase):
    """Для preliminary число оценок в самом лейбле.
    databases = {"default"} — та же причина.
    """

    databases = {"default"}

    def _agg(self, total_votes):
        return SimpleNamespace(
            total_votes=total_votes, stability_index=None,
            own_fans_avg=None, rival_fans_avg=None, neutral_avg=None,
        )

    def test_preliminary_tier_shows_vote_count_in_label(self):
        result = confidence_badge(self._agg(3))
        self.assertEqual(result["tier"], "preliminary")
        # В каждой метке слово «оценок».
        self.assertEqual(result["tier_label"], "Мало оценок · 3")

    def test_basic_tier_label_has_no_inline_count(self):
        result = confidence_badge(self._agg(8))
        self.assertEqual(result["tier"], "basic")
        self.assertEqual(result["tier_label"], "Оценок хватает")

    def test_high_tier_label_has_no_inline_count(self):
        result = confidence_badge(self._agg(20))
        self.assertEqual(result["tier"], "high")
        self.assertEqual(result["tier_label"], "Оценок много")


class HomeTopPlayersVoteGateTests(TestCase):
    """Топ игроков на главной — только с достаточным числом голосов."""

    def setUp(self):
        league = League.objects.create(name="League", country="KZ")
        season, _ = Season.objects.get_or_create(league=league, year="2026")
        home = Team.objects.create(name="Home")
        away = Team.objects.create(name="Away")
        self.match = Match.objects.create(
            league=league, season=season, home_team=home, away_team=away,
            start_time=timezone.now(), status="finished",
            voting_open_until=timezone.now() - timedelta(minutes=1),
        )
        self.underdog = Player.objects.create(first_name="Under", last_name="Dog", team=home)
        self.star = Player.objects.create(first_name="Star", last_name="Player", team=away)

    def test_single_inflated_vote_excluded_from_top_players(self):
        PlayerMatchAggregate.objects.create(
            player=self.underdog, match=self.match,
            performance_score=10.0, total_votes=1,
        )
        PlayerMatchAggregate.objects.create(
            player=self.star, match=self.match,
            performance_score=8.0, total_votes=MIN_VOTES_FOR_DISPLAY,
        )
        response = self.client.get(reverse("core:home"))
        top_players = list(response.context["top_players"])
        self.assertEqual([agg.player_id for agg in top_players], [self.star.id])

    def test_below_threshold_yields_empty_top_players(self):
        PlayerMatchAggregate.objects.create(
            player=self.underdog, match=self.match,
            performance_score=10.0, total_votes=MIN_VOTES_FOR_DISPLAY - 1,
        )
        response = self.client.get(reverse("core:home"))
        self.assertEqual(list(response.context["top_players"]), [])
