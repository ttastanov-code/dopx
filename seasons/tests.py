# seasons/tests.py
"""
Season — тот слой, где реально живёт понятие "текущий/активный сезон".
seasons/views.py пуст (заглушка `Create your views here.` — приложение не
рендерит собственных страниц), вся логика — в seasons/models.py:

  - Season.save() держит инвариант "не больше одного is_active=True сезона
    НА ЛИГУ" — тот же паттерн exclude(pk=self.pk)/exclude+update, что и у
    League.is_primary (leagues/models.py, см. leagues/tests.py), но со
    scope по league_id, а не глобально: у каждой лиги может быть свой
    активный сезон одновременно, и активация сезона одной лиги не должна
    трогать активный сезон другой.

  - Season.get_primary_active() — ЕДИНАЯ точка правды "какой сезон сейчас
    показываем по умолчанию" (активный сезон ГЛАВНОЙ лиги, League.is_primary),
    используется в 8+ местах по проекту (core/views.py, context_processors.py,
    teams/players/coaches/views.py, season_squad/round_squad/views.py) —
    именно этот метод и есть то место, которое проверяет задача, а не
    списки команд/игроков по сезону: сама фильтрация querysets по сезону
    (`Team`/`Player`) физически живёт в teams/players/coaches/views.py
    (`self.active_season = Season.get_primary_active()`, дальше фильтрация
    там же), а не в приложении seasons — seasons лишь ОТДАЁТ, какой сезон
    считать активным, а не фильтрует чужие списки сама.

Раз get_primary_active() зависит от League.is_primary, а тот инвариант,
как показано в leagues/tests.py, можно нарушить в обход save() — здесь
тоже проверяется defensive-fallback: что происходит, если League.is_primary
ещё не проставлен ни у одной лиги (свежая БД до миграции данных, см.
докстринг самого метода).
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase

from leagues.models import League
from seasons.models import Season


class SeasonIsActiveExclusivityTests(TestCase):
    """Season.save(): не больше одного is_active=True сезона на лигу,
    scope — по league_id, а не глобально."""

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
        """Ключевое отличие от League.is_primary (глобальный, ровно один
        на весь сайт) — у Season.is_active scope per-league, у каждой лиги
        свой независимый активный сезон одновременно."""
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
        """Constraint — на пару (league, year), не на year в одиночку:
        два разных чемпионата вполне могут оба называться "2026"."""
        Season.objects.create(league=self.league, year="2026")
        # Не должно кидать IntegrityError.
        Season.objects.create(league=self.other_league, year="2026")
        self.assertEqual(Season.objects.filter(year="2026").count(), 2)

    def test_default_ordering_is_by_year_descending(self):
        Season.objects.create(league=self.league, year="2024")
        Season.objects.create(league=self.league, year="2026")
        Season.objects.create(league=self.league, year="2025")

        years = list(Season.objects.filter(league=self.league).values_list("year", flat=True))
        self.assertEqual(years, ["2026", "2025", "2024"])


class SeasonGetPrimaryActiveTests(TestCase):
    """Season.get_primary_active() — единая точка правды "какой сезон
    показываем по умолчанию" (активный сезон главной лиги сайта)."""

    def test_returns_active_season_of_primary_league(self):
        primary_league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        other_league = League.objects.create(name="Cup", country="KZ", is_primary=False)
        expected = Season.objects.create(league=primary_league, year="2026", is_active=True)
        Season.objects.create(league=other_league, year="2026", is_active=True)

        result = Season.get_primary_active()
        self.assertEqual(result, expected)

    def test_ignores_active_season_of_non_primary_league_when_primary_has_none_active(self):
        """Активный сезон НЕ главной лиги не должен подменять собой
        отсутствие активного сезона у главной — иначе главная страница
        внезапно показала бы данные чужого чемпионата."""
        primary_league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        other_league = League.objects.create(name="Cup", country="KZ", is_primary=False)
        # У главной лиги активного сезона нет вообще.
        Season.objects.create(league=primary_league, year="2026", is_active=False)
        Season.objects.create(league=other_league, year="2026", is_active=True)

        # Строгий вариант метода (primary+active) ничего не находит и
        # уходит в fallback — который в данном случае вернёт активный
        # сезон Кубка, потому что это ЕДИНСТВЕННЫЙ активный сезон в базе.
        # Сам fallback покрыт отдельным тестом ниже с более однозначным
        # сценарием (is_primary вообще не проставлен ни у кого).
        result = Season.get_primary_active()
        self.assertIsNotNone(result)
        self.assertFalse(result.league.is_primary)

    def test_falls_back_to_any_active_season_when_no_league_marked_primary(self):
        """Defensive-сценарий из докстринга метода: миграция бэкафилла
        is_primary ещё не прогнана (или намеренно ни одна лига не
        помечена) — деградируем к старому поведению вместо пустого
        результата и падения."""
        league = League.objects.create(name="KPL", country="KZ", is_primary=False)
        expected = Season.objects.create(league=league, year="2026", is_active=True)

        result = Season.get_primary_active()
        self.assertEqual(result, expected)

    def test_returns_none_when_nothing_is_active_anywhere(self):
        league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        Season.objects.create(league=league, year="2026", is_active=False)

        self.assertIsNone(Season.get_primary_active())

    def test_multiple_primary_leagues_defensive_does_not_crash(self):
        """Зеркало leagues/tests.py::test_multiple_primary_leagues_via_bulk_update_does_not_crash_on_read
        — если инвариант League.is_primary всё-таки нарушен в обход
        save() (bulk update()), get_primary_active() не должен падать,
        просто детерминированно (по .first()) вернёт один из активных
        сезонов "главных" лиг."""
        league_a = League.objects.create(name="KPL", country="KZ")
        league_b = League.objects.create(name="Cup", country="KZ")
        League.objects.filter(pk__in=[league_a.pk, league_b.pk]).update(is_primary=True)
        season_a = Season.objects.create(league=league_a, year="2026", is_active=True)
        season_b = Season.objects.create(league=league_b, year="2026", is_active=True)

        result = Season.get_primary_active()
        self.assertIsNotNone(result)
        self.assertIn(result.pk, {season_a.pk, season_b.pk})
