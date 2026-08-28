# leagues/tests.py
"""
League.is_primary — единственная точка правды "какая лига считается
главной лигой сайта" (турнирная таблица на главной странице, дефолтный
сезон в шапке/виджетах — см. Season.get_primary_active в seasons/models.py,
которая фильтрует именно по league__is_primary). Комментарий на самом поле
прямо требует инвариант "должна быть ровно одна" — но в коде это
обеспечивается ТОЛЬКО в League.save() (переопределённый метод), а не
db-уровневым constraint (partial unique index тут не заведён). Значит любой
путь записи в обход save() — bulk_update(), queryset.update(),
RunSQL-миграция — тихо ломает инвариант и НЕ упадёт ошибкой БД.

Этот файл проверяет:
  1. что save() действительно держит инвариант на обычном пути (админка,
     League.objects.create(), повторное сохранение);
  2. defensive-поведение, когда инвариант всё-таки нарушен в обход save() —
     код не должен падать, даже если строго счастливого пути (ровно одна
     is_primary=True) в БД не оказалось. Это тот сценарий, который явно
     просили проверить в задаче: "при нескольких is_primary=True (не
     должно быть, но проверь defensive-поведение) или ни одной — что
     происходит (crash или fallback)".

leagues/views.py (LeagueListView/LeagueDetailView) сам is_primary не
использует — LeagueDetailView получает лигу из URL (`<uuid:pk>/`), поэтому
уже однозначно скопирован на конкретную лигу и не участвует в выборе
"какая лига главная". Ambiguity, которую снимает is_primary, целиком
относится к Season.get_primary_active() (seasons/tests.py) и к местам без
явного pk в URL (core/views.py, context_processors.py) — они вне этого
приложения. Здесь тестируется модель; один лёгкий тест на
LeagueListView.get_queryset() — просто чтобы убедиться, что список лиг
не зависит от is_primary вообще (сортировка по имени, а не "главная
сверху").
"""
from __future__ import annotations

from django.test import TestCase

from leagues.models import League
from leagues.views import LeagueListView


class LeagueIsPrimaryExclusivityTests(TestCase):
    """Счастливый путь: League.save() гарантирует ровно одну is_primary
    лигу при обычной записи (создание, повторное сохранение, снятие
    флага)."""

    def test_creating_new_primary_unsets_previous_one(self):
        first = League.objects.create(name="KPL", country="KZ", is_primary=True)
        second = League.objects.create(name="Cup", country="KZ", is_primary=True)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_primary, "вторая лига должна была снять флаг с первой")
        self.assertTrue(second.is_primary)
        self.assertEqual(League.objects.filter(is_primary=True).count(), 1)

    def test_resaving_the_primary_league_does_not_unset_itself(self):
        """exclude(pk=self.pk) в League.save() — без него лига сама себя
        сняла бы с флага на каждом повторном сохранении (например, staff
        поменял только `country` в админке, не трогая is_primary)."""
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
        """pk у League — UUIDField(default=uuid.uuid4), заполняется ДО
        INSERT, поэтому exclude(pk=self.pk) корректно работает даже на
        самом первом save() новой записи (self.pk никогда не None)."""
        league = League.objects.create(name="KPL", country="KZ", is_primary=True)
        self.assertTrue(League.objects.filter(pk=league.pk, is_primary=True).exists())


class LeagueIsPrimaryDefensiveTests(TestCase):
    """Инвариант "ровно одна is_primary" держится только в Python-коде
    League.save() — прямой queryset.update() (bulk-операции, ручной SQL,
    гипотетический будущий скрипт миграции данных) может его нарушить в
    обход save(). Ниже — что происходит в этом (не должном, но
    реалистичном) случае: код не должен падать ни при "слишком много",
    ни при "ни одной" is_primary лиги."""

    def test_multiple_primary_leagues_via_bulk_update_does_not_crash_on_read(self):
        first = League.objects.create(name="KPL", country="KZ")
        second = League.objects.create(name="Cup", country="KZ")
        # Bypass League.save() намеренно — bulk update() не вызывает
        # переопределённый save(), это единственный реалистичный способ
        # получить в БД два is_primary=True одновременно.
        League.objects.filter(pk__in=[first.pk, second.pk]).update(is_primary=True)

        self.assertEqual(League.objects.filter(is_primary=True).count(), 2, "update() обходит save(), инвариант нарушен намеренно для теста")

        # Downstream-код (Season.get_primary_active) использует .first() —
        # он не должен кидать исключение из-за MultipleObjectsReturned
        # или чего-то подобного, просто детерминированно вернёт одну из них.
        primary = League.objects.filter(is_primary=True).first()
        self.assertIsNotNone(primary)
        self.assertIn(primary.pk, {first.pk, second.pk})

    def test_no_primary_league_returns_none_without_crashing(self):
        League.objects.create(name="KPL", country="KZ", is_primary=False)
        League.objects.create(name="Cup", country="KZ", is_primary=False)

        self.assertIsNone(League.objects.filter(is_primary=True).first())


class LeagueListViewTests(TestCase):
    """Список лиг сортируется по имени и не зависит от is_primary — в
    отличие от главной страницы, здесь НЕТ выбора "одной активной", это
    просто полный список всех лиг для навигации."""

    def test_queryset_ordered_by_name_regardless_of_primary_flag(self):
        League.objects.create(name="Zeta League", country="KZ", is_primary=True)
        League.objects.create(name="Alpha League", country="KZ", is_primary=False)

        names = list(LeagueListView().get_queryset().values_list("name", flat=True))
        self.assertEqual(names, ["Alpha League", "Zeta League"])
