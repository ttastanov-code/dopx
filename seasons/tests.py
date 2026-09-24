# seasons/tests.py
"""Тесты Season: один активный сезон на лигу (save()) и get_primary_active()
(активный сезон главной лиги) с fallback-сценариями.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase

from leagues.models import League
from seasons.models import Season


class SeasonIsActiveExclusivityTests(TestCase):
    """Не больше одного активного сезона на лигу."""

    def setUp(self):
        self.league = League.objects.create(name="KPL", country="KZ")
        self.other_league = League.objects.create(name="Cup", country="KZ")

    def test_creating_new_active_season_unsets_previous_one_in_same_league(self):
        old = Season.objects.create(league=self.league, year="2025", is_active=True)
        new = Season.objects.create(league=self.league, year="2026", is_active=True)

        old.refresh_from_db()
        new.refresh_from_db()
        self.assertFalse(old.is_active, "новый активный сезон должен снять флаг со старого сезона той же лиги")
        self.assertTrue(new.is_active)
        self.assertEqual(Season.objects.filter(league=self.league, is_active=True).count(), 1)

    def test_activating_season_does_not_affect_other_leagues(self):
        """У разных лиг — свои активные сезоны."""
        own_active = Season.objects.create(league=self.league, year="2026", is_active=True)
        other_active = Season.objects.create(league=self.other_league, year="2026", is_active=True)

        other_active.refresh_from_db()
        self.assertTrue(other_active.is_active, "активация сезона одной лиги не должна деактивировать сезон другой лиги")
        own_active.refresh_from_db()
        self.assertTrue(own_active.is_active)

    def test_resaving_active_season_does_not_unset_itself(self):
        season = Season.objects.create(league=self.league, year="2026", is_active=True)
        season.save()
        season.refresh_from_db()
        self.assertTrue(season.is_active)

    def test_duplicate_league_year_raises_integrity_error(self):
        Season.objects.create(league=self.league, year="2026")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Season.objects.create(league=self.league, year="2026")

    def test_same_year_allowed_for_different_leagues(self):
        """Уникальность — (league, year)."""
        Season.objects.create(league=self.league, year="2026")
        # Без IntegrityError.
        Season.objects.create(league=self.other_league, year="2026")
        self.assertEqual(Season.objects.filter(year="2026").count(), 2)

    def test_default_ordering_is_by_year_descending(self):
        Season.objects.create(league=self.league, year="2024")
        Season.objects.create(league=self.league, year="2026")
        Season.objects.create(league=self.league, year="2025")

        years = list(Season.objects.filter(league=self.league).values_list("year", flat=True))
        self.assertEqual(years, ["2026", "2025", "2024"])


class SeasonGetPrimaryActiveTests(TestCase):
    """get_primary_active — активный сезон главной лиги."""

    def test_returns_active_season_of_primary_league(self):
        primary_league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        other_league = League.objects.create(name="Cup", country="KZ", is_primary=False)
        expected = Season.objects.create(league=primary_league, year="2026", is_active=True)
        Season.objects.create(league=other_league, year="2026", is_active=True)

        result = Season.get_primary_active()
        self.assertEqual(result, expected)

    def test_ignores_active_season_of_non_primary_league_when_primary_has_none_active(self):
        """Активный сезон другой лиги не подменяет главную."""
        primary_league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        other_league = League.objects.create(name="Cup", country="KZ", is_primary=False)
        # У главной лиги нет активного сезона.
        Season.objects.create(league=primary_league, year="2026", is_active=False)
        Season.objects.create(league=other_league, year="2026", is_active=True)

        # Строгий поиск пуст — fallback вернёт единственный активный (Кубка).
        result = Season.get_primary_active()
        self.assertIsNotNone(result)
        self.assertFalse(result.league.is_primary)

    def test_falls_back_to_any_active_season_when_no_league_marked_primary(self):
        """Ни одна лига не главная — fallback на старое поведение."""
        league = League.objects.create(name="KPL", country="KZ", is_primary=False)
        expected = Season.objects.create(league=league, year="2026", is_active=True)

        result = Season.get_primary_active()
        self.assertEqual(result, expected)

    def test_returns_none_when_nothing_is_active_anywhere(self):
        league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        Season.objects.create(league=league, year="2026", is_active=False)

        self.assertIsNone(Season.get_primary_active())

    def test_multiple_primary_leagues_defensive_does_not_crash(self):
        """Несколько главных лиг (в обход save()) — не падаем, берём .first()."""
        league_a = League.objects.create(name="KPL", country="KZ")
        league_b = League.objects.create(name="Cup", country="KZ")
        League.objects.filter(pk__in=[league_a.pk, league_b.pk]).update(is_primary=True)
        season_a = Season.objects.create(league=league_a, year="2026", is_active=True)
        season_b = Season.objects.create(league=league_b, year="2026", is_active=True)

        result = Season.get_primary_active()
        self.assertIsNotNone(result)
        self.assertIn(result.pk, {season_a.pk, season_b.pk})
