# leagues/tests.py
"""Тесты League.is_primary: save() держит ровно одну главную лигу;
нарушение в обход save() (bulk update) не роняет код.
"""
from __future__ import annotations

from django.test import TestCase

from leagues.models import League
from leagues.views import LeagueListView


class LeagueIsPrimaryExclusivityTests(TestCase):
    """save() оставляет одну главную лигу."""

    def test_creating_new_primary_unsets_previous_one(self):
        first = League.objects.create(name="KPL", country="KZ", is_primary=True)
        second = League.objects.create(name="Cup", country="KZ", is_primary=True)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_primary, "вторая лига должна была снять флаг с первой")
        self.assertTrue(second.is_primary)
        self.assertEqual(League.objects.filter(is_primary=True).count(), 1)

    def test_resaving_the_primary_league_does_not_unset_itself(self):
        """Повторное сохранение не снимает флаг с самой лиги."""
        league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        league.country = "Kazakhstan"
        league.save()

        league.refresh_from_db()
        self.assertTrue(league.is_primary, "лига не должна снимать флаг сама с себя при повторном save()")

    def test_unsetting_primary_does_not_affect_other_leagues(self):
        primary = League.objects.create(name="KPL", country="KZ", is_primary=True)
        other = League.objects.create(name="Cup", country="KZ", is_primary=False)

        primary.is_primary = False
        primary.save()

        other.refresh_from_db()
        self.assertFalse(other.is_primary, "save() с is_primary=False не должен трогать чужие лиги вообще")
        self.assertEqual(League.objects.filter(is_primary=True).count(), 0)

    def test_first_ever_league_can_be_primary_without_crashing(self):
        """pk заполнен до INSERT — exclude(pk=self.pk) работает и на первом save()."""
        league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        self.assertTrue(League.objects.filter(pk=league.pk, is_primary=True).exists())


class LeagueIsPrimaryDefensiveTests(TestCase):
    """Инвариант нарушен через update() — код не должен падать."""

    def test_multiple_primary_leagues_via_bulk_update_does_not_crash_on_read(self):
        first = League.objects.create(name="KPL", country="KZ")
        second = League.objects.create(name="Cup", country="KZ")
        # update() в обход save() — два is_primary=True.
        League.objects.filter(pk__in=[first.pk, second.pk]).update(is_primary=True)

        self.assertEqual(League.objects.filter(is_primary=True).count(), 2, "update() обходит save(), инвариант нарушен намеренно для теста")

        # get_primary_active использует .first() — не падает.
        primary = League.objects.filter(is_primary=True).first()
        self.assertIsNotNone(primary)
        self.assertIn(primary.pk, {first.pk, second.pk})

    def test_no_primary_league_returns_none_without_crashing(self):
        League.objects.create(name="KPL", country="KZ", is_primary=False)
        League.objects.create(name="Cup", country="KZ", is_primary=False)

        self.assertIsNone(League.objects.filter(is_primary=True).first())


class LeagueListViewTests(TestCase):
    """Список лиг — по имени, is_primary не влияет."""

    def test_queryset_ordered_by_name_regardless_of_primary_flag(self):
        League.objects.create(name="Zeta League", country="KZ", is_primary=True)
        League.objects.create(name="Alpha League", country="KZ", is_primary=False)

        names = list(LeagueListView().get_queryset().values_list("name", flat=True))
        self.assertEqual(names, ["Alpha League", "Zeta League"])
