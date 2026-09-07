# core/tests.py
"""
is_rate_limited — единственный rate-limiter в проекте (см. docstring
core/utils.py), в этой сессии подключённый ещё к 4 эндпоинтам (регистрация
уже была прикрыта раньше; password-reset, verify-email, toggle_follow,
react_to_event — новые потребители). Ошибка в самой примитиве расползлась
бы сразу на все 5 точек, поэтому логика fixed-window тестируется отдельно,
на уровне core, а не по одному разу в каждом потребителе.

CACHES переопределён на LocMemCache через @override_settings — прод
использует Redis (dopx/settings.py::CACHES), но сама логика is_rate_limited
работает с любым Django cache-backend через одинаковый API (get/set/incr),
так что тест не должен зависеть от того, поднят ли Redis на машине, где
запускается `manage.py test`.
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
from core.utils import is_rate_limited
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


@override_settings(CACHES=LOCMEM_CACHES)
class IsRateLimitedTests(SimpleTestCase):
    def setUp(self):
        # LocMemCache — общий процесс кэша на все тесты класса (LOCATION
        # одна и та же), без ручной очистки между тестами один тест мог бы
        # унаследовать бакет предыдущего и получить ложный False/True.
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
        # 4-й вызов в том же окне — лимит уже исчерпан.
        self.assertTrue(is_rate_limited("k3", limit=3, window_seconds=60))

    def test_different_keys_have_independent_buckets(self):
        for _ in range(3):
            is_rate_limited("bucket_a", limit=3, window_seconds=60)
        # bucket_a исчерпан, но bucket_b — отдельный ключ, свежий бакет.
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
    """
    НАЙДЕНО (2026-09-01, жалоба пользователя: "фанаты игрока тоже неверно
    — это могут быть фанаты команды, а не конкретного игрока" + "описание
    вообще нихера непонятное"): `own_fans_avg`/`rival_fans_avg` — это
    фанаты КОМАНДЫ сущности (aggregates/services.py::
    segment_evaluations_by_side, entity_team_id), не персонально игрока.
    Поле есть не только на PlayerMatchAggregate, но и на
    TeamMatchAggregate/CoachMatchAggregate (aggregates/models.py) — старая
    подпись "фанаты игрока" была прямо неверной для рейтинга команды/
    тренера, не только неточной для игрока. Используем SimpleNamespace
    вместо реальных Django-моделей — bias_segment_text читает только эти
    три плоских поля, полноценная фикстура с БД не нужна.
    """

    def _agg(self, own=None, rival=None, neutral=None):
        return SimpleNamespace(own_fans_avg=own, rival_fans_avg=rival, neutral_avg=neutral)

    def test_no_generic_player_wording_used(self):
        """Регрессия на буквальную формулировку из жалобы — "фанаты игрока"
        не должно встречаться вообще, независимо от типа сущности."""
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
        """Один сегмент — сравнивать не с чем, подсказка была бы бесполезна."""
        self.assertEqual(bias_segment_text(self._agg(own=8.0)), "")
        self.assertEqual(bias_segment_text(None), "")

    def test_two_segments_is_enough(self):
        text = bias_segment_text(self._agg(own=8.0, rival=7.0))
        self.assertTrue(text)


class StabilityLabelTests(SimpleTestCase):
    def test_label_does_not_repeat_word_mneniya(self):
        """
        НАЙДЕНО (2026-09-01): раньше stability_label() возвращала "мнения
        расходятся" целиком, а confidence_badge() собирал "Разброс мнений:
        {label}" — получалось задвоенное "мнения мнения" по смыслу
        ("Разброс мнений: мнения расходятся"). Слово "мнения" теперь
        только в confidence_badge, сама метка — просто прилагательное.
        """
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
    """confidence_badge() склеивает stability_label + bias_segment_text в
    ОДНО предложение, когда есть оба — регрессия на "Разброс мнений: мнения
    расходятся. Разбивка по лагерям — фанаты игрока: ..." (двумя корявыми
    фрагментами, см. докстринг confidence_badge в rating_extras.py)."""

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
        # Не должно остаться старого задвоения "Разброс мнений: мнения ...".
        self.assertNotIn("мнения мнения", tooltip.lower())

    def test_none_aggregate_hides_badge(self):
        self.assertEqual(confidence_badge(None), {"show": False})


class ConfidenceBadgeSampleSizeTests(SimpleTestCase):
    """Число оценок — прямо в видимом лейбле бейджа для preliminary-уровня
    (docs/BACKLOG.md: "показывать число оценок и пометку 'предварительный
    рейтинг' при маленькой выборке"), не только в тултипе по наведению."""

    def _agg(self, total_votes):
        return SimpleNamespace(
            total_votes=total_votes, stability_index=None,
            own_fans_avg=None, rival_fans_avg=None, neutral_avg=None,
        )

    def test_preliminary_tier_shows_vote_count_in_label(self):
        result = confidence_badge(self._agg(3))
        self.assertEqual(result["tier"], "preliminary")
        self.assertEqual(result["tier_label"], "Предварительно · 3")

    def test_basic_tier_label_has_no_inline_count(self):
        result = confidence_badge(self._agg(8))
        self.assertEqual(result["tier"], "basic")
        self.assertEqual(result["tier_label"], "Есть данные")

    def test_high_tier_label_has_no_inline_count(self):
        result = confidence_badge(self._agg(20))
        self.assertEqual(result["tier"], "high")
        self.assertEqual(result["tier_label"], "Высокая надёжность")


class HomeTopPlayersVoteGateTests(TestCase):
    """HomeView.top_players — тот же класс проблемы, что чинили для топа
    игроков команды (docs/adr/0014-team-top-players-transfer-fix.md):
    без гейта по total_votes единичный накрученный голос обходит в топе
    игрока с честными десятками оценок."""

    def setUp(self):
        league = League.objects.create(name="League", country="KZ")
        season, _ = Season.objects.get_or_create(league=league, year="2026")
        home = Team.objects.create(name="Home")
        away = Team.objects.create(name="Away")
        self.match = Match.objects.create(
            league=league, season=season, home_team=home, away_team=away,
            start_time=timezone.now(), status="finished",
            voting_open_until=timezone.now() + timedelta(hours=48),
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
