# matches/tests_card_services.py
"""
Регрессионные тесты редизайна карточки матча (2026-09-10, прямая просьба
пользователя, полный бриф из 14 пунктов — "делай всё сразу, без косяков").

Пользователь несколько раз за эту сессию ловил регрессии от "исправлений
методом тыка" без тестов (транслитерация, apply_cyrillic_names) — эта
секция сессии написана с самого начала СРАЗУ с тестами на самую рискованную
логику: приоритет тегов интриги/CTA/ключевого момента, формула индекса
сенсации, алгоритм "было/стало" в таблице (должен ТОЧНО совпадать с
aggregates/tasks.py::_recalculate_standings_for_season — иначе факт "поднялся
на N место" будет противоречить настоящей турнирной таблице сайта).

Запуск: python manage.py test matches.tests_card_services
"""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from aggregates.models import MatchAggregate, PlayerMatchAggregate
from events.models import MatchEvent
from leagues.models import League
from matches.card_services import attach_card_extras
from matches.models import Match, MatchReaction
from matches.services import (
    compute_sensation_index,
    describe_card_dna_traits,
    describe_finished_cta,
    describe_intrigue,
    describe_key_moment,
    describe_reaction_badge,
    describe_table_impact,
    reaction_counts,
    submit_match_reaction,
    top_reaction_matches,
    user_match_reaction,
)
from players.models import Player
from seasons.models import Season
from teams.models import Team, TeamSeason, TeamSeasonStats
from teams.services import compute_standings_asof, describe_form_streak, describe_season_form_streak
from users.models import User


class CardServicesTestCase(TestCase):
    """Общая фикстура — тот же приём прямого построения через ORM, что и
    predictions/tests.py::PredictionsTestCase (см. её докстринг за
    обоснованием: быстрее и точнее полного HTTP-цикла)."""

    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team_a = Team.objects.create(name="Алатау")
        self.team_b = Team.objects.create(name="Бирлик")
        self._user_counter = 0

    def make_match(self, status='scheduled', start_time=None, home_score=None, away_score=None,
                    home_team=None, away_team=None, decided_administratively=False):
        start_time = start_time if start_time is not None else timezone.now() + timedelta(days=2)
        return Match.objects.create(
            league=self.league, season=self.season,
            home_team=home_team or self.team_a, away_team=away_team or self.team_b,
            status=status, start_time=start_time,
            voting_open_until=start_time + timedelta(days=3),
            home_score=home_score, away_score=away_score,
            decided_administratively=decided_administratively,
        )

    def make_user(self, name="user"):
        self._user_counter += 1
        return User.objects.create_user(
            username=f"{name}{self._user_counter}", email=f"{name}{self._user_counter}@test.local",
            password="testpass123",
        )

    def make_player(self, team, first_name="Игрок", last_name="Тестовый"):
        return Player.objects.create(team=team, first_name=first_name, last_name=last_name)


class DescribeFormStreakTests(TestCase):
    """teams/services.py::describe_form_streak — чистая функция, без БД."""

    def _entry(self, result):
        return {'result': result, 'match': None, 'opponent': None, 'is_home': True, 'score_display': ''}

    def test_none_for_empty_form(self):
        self.assertIsNone(describe_form_streak([]))

    def test_win_streak_text(self):
        # get_team_form() отдаёт СТАРЫЕ -> НОВЫЕ (см. её докстринг) — три
        # победы подряд, самая свежая последняя.
        form = [self._entry('L'), self._entry('W'), self._entry('W'), self._entry('W')]
        self.assertEqual(describe_form_streak(form), '3 победы подряд')

    def test_single_win_is_not_a_streak_but_still_reported(self):
        form = [self._entry('L'), self._entry('D'), self._entry('W')]
        self.assertEqual(describe_form_streak(form), 'Победа в последнем матче')

    def test_non_win_streak_text(self):
        form = [self._entry('W'), self._entry('L'), self._entry('D')]
        self.assertEqual(describe_form_streak(form), 'Не побеждает 2 матча')

    def test_single_non_win_is_not_a_streak(self):
        form = [self._entry('W'), self._entry('W'), self._entry('L')]
        self.assertIsNone(describe_form_streak(form))

    def test_five_win_streak_plural_suffix(self):
        form = [self._entry('W')] * 5
        self.assertEqual(describe_form_streak(form), '5 побед подряд')


class DescribeSeasonFormStreakTests(CardServicesTestCase):
    """ИСПРАВЛЕНО (2026-09-11, прямая просьба пользователя — "стрик из
    побед только в рамках 5 матчей пишем, хотя по факту в этом сезоне
    стрик из 10 побед подряд"): describe_form_streak(get_team_form(...))
    искусственно резал историю до TEAM_FORM_RECENT_MATCHES=5.
    describe_season_form_streak должна считать по ВСЕМ переданным матчам,
    без этого среза."""

    def test_streak_longer_than_five_is_reported_in_full(self):
        # Опонент здесь — self.team_b: в отличие от ComputeStandingsAsofTests
        # (где важна изоляция через общего "мальчика для битья", см. её
        # докстринг), тут считаем серию только team_a — кто соперник,
        # не имеет значения, отдельный team_c не нужен.
        cutoff = timezone.now()
        matches = []
        for i in range(10, 0, -1):
            matches.append(self.make_match(
                status='finished', start_time=cutoff - timedelta(days=i),
                home_team=self.team_a, away_team=self.team_b, home_score=2, away_score=0,
            ))
        # describe_season_form_streak ожидает -start_time (новые первыми).
        matches_desc = list(reversed(matches))
        self.assertEqual(
            describe_season_form_streak(self.team_a, matches_desc),
            '10 побед подряд',
        )

    def test_streak_broken_by_older_loss_stops_there(self):
        cutoff = timezone.now()
        # От старых к новым: поражение, потом 6 побед подряд.
        loss = self.make_match(
            status='finished', start_time=cutoff - timedelta(days=7),
            home_team=self.team_a, away_team=self.team_b, home_score=0, away_score=2,
        )
        wins = [
            self.make_match(
                status='finished', start_time=cutoff - timedelta(days=6 - i),
                home_team=self.team_a, away_team=self.team_b, home_score=2, away_score=0,
            )
            for i in range(6)
        ]
        matches_desc = list(reversed([loss] + wins))
        self.assertEqual(
            describe_season_form_streak(self.team_a, matches_desc),
            '6 побед подряд',
        )

    def test_empty_matches_returns_none(self):
        self.assertIsNone(describe_season_form_streak(self.team_a, []))


class ComputeStandingsAsofTests(CardServicesTestCase):
    """ГЛАВНЫЙ РЕГРЕССИОННЫЙ ТЕСТ — compute_standings_asof() должен считать
    позиции ТОЧНО тем же алгоритмом, что настоящий пересчёт таблицы
    (aggregates/tasks.py::_recalculate_standings_for_season): сортировка
    -points/-goal_diff/-goals_scored. Если когда-нибудь кто-то поменяет
    один алгоритм и забудет другой — карточка матча будет врать про
    "поднялся на N место" вопреки настоящей турнирной таблице сайта."""

    def setUp(self):
        super().setUp()
        self.team_c = Team.objects.create(name="Целинник")
        for team in (self.team_a, self.team_b, self.team_c):
            TeamSeason.objects.create(team=team, season=self.season)

    def test_position_only_counts_matches_before_cutoff(self):
        cutoff = timezone.now()
        # Матч ДО cutoff: A обыгрывает B 2:0 — A получает 3 очка.
        self.make_match(
            status='finished', start_time=cutoff - timedelta(days=10),
            home_score=2, away_score=0,
        )
        # Матч ПОСЛЕ cutoff: не должен учитываться в "было".
        self.make_match(
            status='finished', start_time=cutoff + timedelta(days=1),
            home_team=self.team_b, away_team=self.team_c, home_score=5, away_score=0,
        )
        positions = compute_standings_asof(self.season, cutoff)
        self.assertEqual(positions[self.team_a.id], 1)  # 3 очка
        # B и C — по 0 очков на момент cutoff, разница мячей 0:0 у обоих —
        # порядок между ними не гарантирован (тай-брейк дальше не задан),
        # важно только что A строго впереди.
        self.assertGreater(positions[self.team_b.id], positions[self.team_a.id])
        self.assertGreater(positions[self.team_c.id], positions[self.team_a.id])

    def test_tie_break_matches_goal_diff_then_goals_scored(self):
        # ИСПРАВЛЕНО (баг найден пользователем реальным прогоном — assertEqual
        # 2 != 1): у обоих матчей ниже ОБЩИЙ противник (team_c), а не team_b
        # против team_a напрямую — раньше первый матч был "A против B", а
        # значит A и B оба УЖЕ участвовали в нём (A победитель, B проигравший)
        # ещё ДО второго матча "B против C" — ожидаемые очки/разница мячей в
        # комментариях не учитывали это участие team_b в первом матче, из-за
        # чего ожидание теста было арифметически неверным (сама функция
        # compute_standings_asof считала правильно, ошибка была в тесте).
        # Теперь team_a и team_b встречаются только с общим "мальчиком для
        # битья" team_c — их результаты друг на друга не влияют, тай-брейк
        # проверяется чисто.
        cutoff = timezone.now()
        # A громит C 3:1 -> A: points=3, gd=+2, gs=3.
        self.make_match(
            status='finished', start_time=cutoff - timedelta(days=5),
            home_team=self.team_a, away_team=self.team_c, home_score=3, away_score=1,
        )
        # B громит C 4:1 -> B: points=3, gd=+3, gs=4. Очков поровну (3=3),
        # но goal_diff у B (+3) больше, чем у A (+2) -> B должен быть выше A.
        self.make_match(
            status='finished', start_time=cutoff - timedelta(days=4),
            home_team=self.team_b, away_team=self.team_c, home_score=4, away_score=1,
        )
        positions = compute_standings_asof(self.season, cutoff)
        self.assertEqual(positions[self.team_b.id], 1)
        self.assertEqual(positions[self.team_a.id], 2)

    def test_team_with_no_matches_yet_still_included(self):
        cutoff = timezone.now()
        positions = compute_standings_asof(self.season, cutoff)
        self.assertIn(self.team_c.id, positions)


class DescribeIntrigueTests(CardServicesTestCase):
    def test_derby_wins_over_everything_else(self):
        self.team_a.rivals.add(self.team_b)
        match = self.make_match()
        tag = describe_intrigue(
            match, home_position=1, away_position=2, total_teams=16,
            last_meeting={'home_team_id': self.team_b.id, 'away_team_id': self.team_a.id, 'home_score': 4, 'away_score': 0},
        )
        self.assertEqual(tag, 'Дерби')

    def test_top_battle_tag(self):
        match = self.make_match()
        tag = describe_intrigue(match, home_position=2, away_position=3, total_teams=16)
        self.assertEqual(tag, 'Битва за топ-3')

    def test_relegation_battle_tag(self):
        match = self.make_match()
        # total_teams=16, зона вылета — 14,15,16
        tag = describe_intrigue(match, home_position=15, away_position=16, total_teams=16)
        self.assertEqual(tag, 'Матч за выживание')

    def test_revenge_tag_needs_big_margin(self):
        match = self.make_match()
        # Разгром 4:0 — прошлый раз домашняя команда ТЕКУЩЕГО матча (team_a)
        # играла в гостях и проиграла 0:4.
        last_meeting = {
            'home_team_id': self.team_b.id, 'away_team_id': self.team_a.id,
            'home_score': 4, 'away_score': 0,
        }
        tag = describe_intrigue(match, last_meeting=last_meeting)
        # ИСПРАВЛЕНО (2026-09-10, жалоба пользователя — "Реванш за 0:4" не
        # называл, кто именно жаждёт реванша): теперь тег называет
        # проигравшую тогда команду явно — team_a ("Алатау") тогда играла
        # в гостях и проиграла 0:4.
        self.assertEqual(tag, 'Реванш Алатау за 0:4')

    def test_small_margin_is_not_a_revenge(self):
        match = self.make_match()
        last_meeting = {
            'home_team_id': self.team_b.id, 'away_team_id': self.team_a.id,
            'home_score': 1, 'away_score': 0,
        }
        self.assertIsNone(describe_intrigue(match, last_meeting=last_meeting))

    def test_none_when_nothing_applies(self):
        match = self.make_match()
        self.assertIsNone(describe_intrigue(match, home_position=8, away_position=9, total_teams=16))


class DescribeKeyMomentTests(CardServicesTestCase):
    def test_late_goal_wins_priority(self):
        match = self.make_match(status='finished', home_score=2, away_score=1)
        player = self.make_player(self.team_a)
        MatchEvent.objects.create(match=match, minute=10, event_type='goal', team_side='home', player=player)
        MatchEvent.objects.create(match=match, minute=88, event_type='goal', team_side='home', player=player)
        events = list(match.events.order_by('minute'))
        text = describe_key_moment(match, events)
        self.assertIn("88", text)
        self.assertIn("решил исход матча", text)

    def test_red_card_when_no_late_goal(self):
        match = self.make_match(status='finished', home_score=1, away_score=1)
        player = self.make_player(self.team_a)
        MatchEvent.objects.create(match=match, minute=30, event_type='goal', team_side='home', player=player)
        MatchEvent.objects.create(match=match, minute=45, event_type='red_card', team_side='away', player=player)
        events = list(match.events.order_by('minute'))
        text = describe_key_moment(match, events)
        self.assertIn("Красная карточка", text)

    def test_decided_administratively_returns_none(self):
        match = self.make_match(status='finished', decided_administratively=True)
        self.assertIsNone(describe_key_moment(match, []))

    def test_no_notable_events_returns_none(self):
        match = self.make_match(status='finished', home_score=0, away_score=0)
        self.assertIsNone(describe_key_moment(match, []))


class DescribeCardDnaTraitsTests(CardServicesTestCase):
    def test_no_traits_below_min_votes(self):
        match = self.make_match(status='finished')
        agg = MatchAggregate.objects.create(match=match, total_votes=1, drama_index=80)
        self.assertEqual(describe_card_dna_traits(agg), [])

    def test_high_drama_trait(self):
        match = self.make_match(status='finished')
        agg = MatchAggregate.objects.create(
            match=match, total_votes=10, drama_index=70, avg_fairness=7, turning_point_ratio=0.1,
        )
        self.assertIn('Высокая драма', describe_card_dna_traits(agg))

    def test_none_aggregate_returns_empty_list(self):
        self.assertEqual(describe_card_dna_traits(None), [])


class ComputeSensationIndexTests(CardServicesTestCase):
    def test_none_when_not_enough_predictions(self):
        match = self.make_match(status='finished', home_score=2, away_score=0)
        counts = {'total': 2, 'home_pct': 50, 'draw_pct': 0, 'away_pct': 50}
        self.assertIsNone(compute_sensation_index(match, counts))

    def test_none_when_favorite_was_correct(self):
        match = self.make_match(status='finished', home_score=2, away_score=0)  # final_result = '1'
        counts = {'total': 20, 'home_pct': 70, 'draw_pct': 15, 'away_pct': 15}
        self.assertIsNone(compute_sensation_index(match, counts))

    def test_sensation_when_favorite_was_wrong(self):
        match = self.make_match(status='finished', home_score=0, away_score=2)  # final_result = '2'
        counts = {'total': 20, 'home_pct': 65, 'draw_pct': 15, 'away_pct': 20}
        self.assertEqual(compute_sensation_index(match, counts), 65)

    def test_none_for_unfinished_match(self):
        match = self.make_match(status='scheduled')
        counts = {'total': 20, 'home_pct': 65, 'draw_pct': 15, 'away_pct': 20}
        self.assertIsNone(compute_sensation_index(match, counts))

    # --- Доп. предложение (2026-09-10) — реакции как запасной источник,
    # когда прогнозов до матча было мало. ---

    def test_reaction_fallback_used_when_too_few_predictions(self):
        match = self.make_match(status='finished', home_score=0, away_score=2)
        counts = {'total': 2, 'home_pct': 70, 'draw_pct': 10, 'away_pct': 20}  # мало прогнозов
        reaction_counts_dict = {
            'match_of_round': 0, 'upset': 4, 'boring': 1, 'total': 5,
            'match_of_round_pct': 0, 'upset_pct': 80, 'boring_pct': 20,
        }
        self.assertEqual(compute_sensation_index(match, counts, reaction_counts_dict), 80)

    def test_reaction_fallback_ignored_when_predictions_sufficient(self):
        # Прогнозов достаточно и фаворит угадан — сенсации нет, даже если
        # реакции сообщества почему-то говорят об обратном (прогнозы до
        # матча остаются основным источником при достаточной выборке).
        match = self.make_match(status='finished', home_score=2, away_score=0)  # final_result = '1'
        counts = {'total': 20, 'home_pct': 70, 'draw_pct': 15, 'away_pct': 15}
        reaction_counts_dict = {
            'match_of_round': 0, 'upset': 4, 'boring': 1, 'total': 5,
            'match_of_round_pct': 0, 'upset_pct': 80, 'boring_pct': 20,
        }
        self.assertIsNone(compute_sensation_index(match, counts, reaction_counts_dict))

    def test_reaction_fallback_requires_min_votes_and_plurality(self):
        match = self.make_match(status='finished', home_score=0, away_score=2)
        counts = {'total': 2, 'home_pct': 70, 'draw_pct': 10, 'away_pct': 20}
        # Мало голосов реакции (< REACTION_BADGE_MIN_VOTES=5) — не считаем.
        few_votes = {
            'match_of_round': 0, 'upset': 2, 'boring': 0, 'total': 2,
            'match_of_round_pct': 0, 'upset_pct': 100, 'boring_pct': 0,
        }
        self.assertIsNone(compute_sensation_index(match, counts, few_votes))
        # Голосов достаточно, но "Неожиданно" не в большинстве — не считаем.
        no_plurality = {
            'match_of_round': 3, 'upset': 2, 'boring': 0, 'total': 5,
            'match_of_round_pct': 60, 'upset_pct': 40, 'boring_pct': 0,
        }
        self.assertIsNone(compute_sensation_index(match, counts, no_plurality))


class DescribeReactionBadgeTests(CardServicesTestCase):
    def test_none_below_min_votes(self):
        counts = {
            'match_of_round': 3, 'upset': 0, 'boring': 0, 'total': 3,
            'match_of_round_pct': 100, 'upset_pct': 0, 'boring_pct': 0,
        }
        self.assertIsNone(describe_reaction_badge(counts))

    def test_none_when_another_reaction_has_more_votes(self):
        # match_of_round_pct=40 формально проходит порог REACTION_BADGE_MIN_PCT,
        # но по факту голосов МЕНЬШЕ, чем у upset (2 против 3) — не плюральность,
        # округление процента здесь вводило бы в заблуждение.
        counts = {
            'match_of_round': 2, 'upset': 3, 'boring': 0, 'total': 5,
            'match_of_round_pct': 40, 'upset_pct': 60, 'boring_pct': 0,
        }
        self.assertIsNone(describe_reaction_badge(counts))

    def test_badge_when_clear_majority(self):
        counts = {
            'match_of_round': 4, 'upset': 1, 'boring': 0, 'total': 5,
            'match_of_round_pct': 80, 'upset_pct': 20, 'boring_pct': 0,
        }
        self.assertEqual(describe_reaction_badge(counts), 'Матч тура по мнению болельщиков')

    def test_none_when_boring_dominates(self):
        counts = {
            'match_of_round': 1, 'upset': 0, 'boring': 4, 'total': 5,
            'match_of_round_pct': 20, 'upset_pct': 0, 'boring_pct': 80,
        }
        self.assertIsNone(describe_reaction_badge(counts))


class TopReactionMatchesTests(CardServicesTestCase):
    def test_orders_by_vote_count_not_percent(self):
        # match_small: 1 голос из 1 (100%). match_big: 4 голоса из 5 (80%).
        match_small = self.make_match(status='finished', home_score=1, away_score=0)
        match_big = self.make_match(status='finished', home_score=2, away_score=1)
        submit_match_reaction(user=self.make_user(), match=match_small, reaction=MatchReaction.REACTION_MATCH_OF_ROUND)
        for _ in range(4):
            submit_match_reaction(user=self.make_user(), match=match_big, reaction=MatchReaction.REACTION_MATCH_OF_ROUND)
        submit_match_reaction(user=self.make_user(), match=match_big, reaction=MatchReaction.REACTION_BORING)

        top = top_reaction_matches(self.season, MatchReaction.REACTION_MATCH_OF_ROUND, min_votes=1)
        self.assertEqual([m.id for m in top], [match_big.id, match_small.id])

    def test_respects_min_votes_gate(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        submit_match_reaction(user=self.make_user(), match=match, reaction=MatchReaction.REACTION_UPSET)
        top = top_reaction_matches(self.season, MatchReaction.REACTION_UPSET, min_votes=5)
        self.assertEqual(top, [])

    def test_empty_when_no_reactions(self):
        top = top_reaction_matches(self.season, MatchReaction.REACTION_BORING)
        self.assertEqual(top, [])


class DescribeTableImpactTests(CardServicesTestCase):
    def test_moved_up(self):
        text = describe_table_impact(self.team_a, before_position=7, current_position=4)
        self.assertEqual(text, 'Алатау поднялся на 4-е место')

    def test_moved_down(self):
        text = describe_table_impact(self.team_a, before_position=4, current_position=7)
        self.assertEqual(text, 'Алатау опустился на 7-е место')

    def test_no_change_returns_none(self):
        self.assertIsNone(describe_table_impact(self.team_a, before_position=5, current_position=5))

    def test_missing_position_returns_none(self):
        self.assertIsNone(describe_table_impact(self.team_a, before_position=None, current_position=5))


class DescribeFinishedCtaTests(TestCase):
    def test_has_hero_or_dna_gets_analytical_cta(self):
        self.assertEqual(describe_finished_cta(has_hero=True, has_dna=False)['label'], 'Разобрать матч')
        self.assertEqual(describe_finished_cta(has_hero=False, has_dna=True)['label'], 'Разобрать матч')

    def test_no_data_gets_fallback_cta(self):
        self.assertEqual(describe_finished_cta(has_hero=False, has_dna=False)['label'], 'Смотреть оценки игроков')


class MatchReactionServiceTests(CardServicesTestCase):
    def test_submit_reaction_requires_finished_match(self):
        match = self.make_match(status='scheduled')
        user = self.make_user()
        result = submit_match_reaction(user=user, match=match, reaction=MatchReaction.REACTION_BORING)
        self.assertIsNone(result)
        self.assertEqual(MatchReaction.objects.count(), 0)

    def test_submit_and_change_reaction(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        user = self.make_user()
        submit_match_reaction(user=user, match=match, reaction=MatchReaction.REACTION_BORING)
        submit_match_reaction(user=user, match=match, reaction=MatchReaction.REACTION_MATCH_OF_ROUND)
        # Один пользователь — одна (актуальная) запись, не две.
        self.assertEqual(MatchReaction.objects.filter(match=match, user=user).count(), 1)
        stored = user_match_reaction(user, match)
        self.assertEqual(stored.reaction, MatchReaction.REACTION_MATCH_OF_ROUND)

    def test_reaction_counts_percentages(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        for i in range(3):
            submit_match_reaction(user=self.make_user(), match=match, reaction=MatchReaction.REACTION_MATCH_OF_ROUND)
        submit_match_reaction(user=self.make_user(), match=match, reaction=MatchReaction.REACTION_BORING)
        counts = reaction_counts(match)
        self.assertEqual(counts['total'], 4)
        self.assertEqual(counts['match_of_round_pct'], 75)
        self.assertEqual(counts['boring_pct'], 25)


class ReactToMatchViewTests(CardServicesTestCase):
    def test_anonymous_gets_login_prompt_with_200(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        response = self.client.post(reverse('matches:react', args=[match.id]), {'reaction': 'boring'})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'login', response.content.lower())

    def test_invalid_reaction_value_returns_400(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        user = self.make_user()
        self.client.force_login(user)
        response = self.client.post(reverse('matches:react', args=[match.id]), {'reaction': 'not-a-real-choice'})
        self.assertEqual(response.status_code, 400)

    def test_valid_reaction_creates_row(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        user = self.make_user()
        self.client.force_login(user)
        response = self.client.post(reverse('matches:react', args=[match.id]), {'reaction': 'match_of_round'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(MatchReaction.objects.filter(match=match, user=user, reaction='match_of_round').exists())

    def test_get_not_allowed(self):
        match = self.make_match(status='finished', home_score=1, away_score=0)
        response = self.client.get(reverse('matches:react', args=[match.id]))
        self.assertEqual(response.status_code, 405)


class AttachCardExtrasIntegrationTests(CardServicesTestCase):
    """Не проверяет каждую цифру (это уже покрыто юнит-тестами выше) —
    смоук-тест на то, что attach_card_extras() не падает на смешанном
    наборе матчей (scheduled + finished) и реально навешивает ожидаемые
    атрибуты, которые читает шаблон components/_match_card.html."""

    def _request(self, user=None):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = user or self.make_user()
        return request

    def test_does_not_crash_on_mixed_batch_and_sets_attributes(self):
        for team in (self.team_a, self.team_b):
            TeamSeason.objects.create(team=team, season=self.season)
            TeamSeasonStats.objects.create(team=team, season=self.season, position=1)

        upcoming = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=3))
        finished = self.make_match(
            status='finished', start_time=timezone.now() - timedelta(days=1),
            home_score=2, away_score=1,
        )
        player = self.make_player(self.team_a)
        PlayerMatchAggregate.objects.create(
            match=finished, player=player, total_votes=10, performance_score=8.5,
        )
        MatchAggregate.objects.create(match=finished, total_votes=5, drama_index=50, turning_point_ratio=0.5)

        attach_card_extras([upcoming, finished], self._request())

        # Не падает и проставляет базовые атрибуты, которые читает шаблон.
        self.assertTrue(upcoming.card_home_form_text is None or isinstance(upcoming.card_home_form_text, str))
        self.assertIsInstance(finished.card_dna_traits, list)
        self.assertIsNotNone(finished.card_hero)
        self.assertEqual(finished.card_hero['player'].id, player.id)
        self.assertIn(finished.card_cta['label'], ('Разобрать матч', 'Смотреть оценки игроков'))
        self.assertFalse(finished.user_has_evaluated)

    def test_empty_list_is_a_noop(self):
        # Не должно падать на пустом списке (граница, которую легко забыть).
        attach_card_extras([], self._request())
