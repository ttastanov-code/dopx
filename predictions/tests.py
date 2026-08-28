# predictions/tests.py
"""
Краудсорс-прогнозы 1X2 (predictions) не имели ни одного теста — задача из
бэклога "два приложения без единого теста" (2026-08-28), наряду с
round_squad. Ядро логики (окно приёма прогноза, запрет смены прогноза
после старта матча, сверка "прогноз vs итог", консенсус сообщества)
целиком живёт в predictions/services.py и matches/models.py::Match — тесты
целятся именно туда, тем же приёмом прямого построения фикстур через ORM,
что и в core/tests.py/season_squad/tests.py (быстрее и точнее, чем гонять
полный HTTP-цикл через predict()).

View-тесты внизу файла (PredictWidgetViewTests) намеренно ОГРАНИЧЕНЫ путями,
которые не требуют Celery/Redis (анонимный пользователь, невалидный choice,
GET виджета) — успешный POST аутентифицированного пользователя в
predict() ставит track_event(...).delay() и, при первом прогнозе на матч,
check_and_award_badges_task.delay() через transaction.on_commit (см.
predictions/views.py) — реальная логика бейджей/аналитики тестируется в
своих собственных приложениях (users/tests.py, analytics/tests.py), здесь
дублировать её смысла нет, а гонять её "eager" ради одного теста этого
файла добавило бы хрупкости без выигрыша в покрытии predictions-специфичной
логики.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from leagues.models import League
from matches.models import Match
from predictions.models import MatchPrediction
from predictions.services import prediction_counts, submit_prediction, user_prediction
from seasons.models import Season
from teams.models import Team
from users.models import User

LOCMEM_CACHE = {
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
}


class PredictionsTestCase(TestCase):
    """Общая фикстура: лига/сезон/две команды + хелперы для быстрой сборки
    матча в нужной фазе жизненного цикла и пользователя."""

    def setUp(self):
        self.league = League.objects.create(name="Test League", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.team_a = Team.objects.create(name="Team A")
        self.team_b = Team.objects.create(name="Team B")
        self._user_counter = 0

    def make_match(self, status='scheduled', start_time=None, home_score=None, away_score=None):
        start_time = start_time if start_time is not None else timezone.now() + timedelta(days=2)
        return Match.objects.create(
            league=self.league,
            season=self.season,
            home_team=self.team_a,
            away_team=self.team_b,
            status=status,
            start_time=start_time,
            # voting_open_until не участвует в логике прогнозов (это окно
            # для evaluations, см. Match.is_voting_open) — значение не
            # важно для этих тестов, лишь бы поле NOT NULL было заполнено.
            voting_open_until=start_time + timedelta(hours=2),
            home_score=home_score,
            away_score=away_score,
        )

    def make_user(self, name="user"):
        # БАГ, КОТОРЫЙ ТУТ БЫЛ (найден реальным прогоном против Postgres,
        # 2026-08-28): без явного email оба вызова передавали пустую строку
        # по умолчанию — User.email unique=True на уровне БД, так что второй
        # же вызов make_user() в тесте с несколькими пользователями падал
        # IntegrityError'ом "duplicate key... users_user_email_key". В
        # SQLite/локальной песочнице без реальной Postgres это не всплывало.
        self._user_counter += 1
        return User.objects.create_user(
            username=f"{name}{self._user_counter}",
            email=f"{name}{self._user_counter}@test.local",
            password="testpass123",
        )


class PredictionWindowTests(PredictionsTestCase):
    """PREDICTION_WINDOW_DAYS=5 (matches/models.py::Match.is_prediction_open) —
    продуктовое требование 2026-08-21: прогноз нельзя отдать ни слишком
    рано (обесценивает механику "прогноз незадолго до игры"), ни после
    старта матча (см. также CannotChangePredictionAfterStartTests ниже)."""

    def test_too_early_prediction_is_rejected(self):
        """Матч через 6 дней — окно открывается только за 5 дней до старта,
        сейчас ещё рано."""
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=6))
        user = self.make_user()

        prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        self.assertIsNone(prediction)
        self.assertFalse(created)
        self.assertEqual(MatchPrediction.objects.count(), 0)

    def test_prediction_within_window_is_accepted(self):
        """Матч через 4 дня — уже внутри 5-дневного окна и матч ещё не
        начался: прогноз должен приниматься."""
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=4))
        user = self.make_user()

        prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_DRAW)

        self.assertIsNotNone(prediction)
        self.assertTrue(created)
        self.assertEqual(prediction.choice, MatchPrediction.CHOICE_DRAW)
        self.assertEqual(MatchPrediction.objects.count(), 1)

    def test_prediction_after_match_started_is_rejected(self):
        """status='live' — матч уже идёт, окно прогноза закрыто (верхняя
        граница is_prediction_open — сам факт старта, не только время)."""
        match = self.make_match(status='live', start_time=timezone.now() - timedelta(minutes=10))
        user = self.make_user()

        prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_AWAY)

        self.assertIsNone(prediction)
        self.assertFalse(created)
        self.assertEqual(MatchPrediction.objects.count(), 0)

    def test_prediction_after_match_finished_is_rejected(self):
        match = self.make_match(
            status='finished', start_time=timezone.now() - timedelta(days=1),
            home_score=2, away_score=0,
        )
        user = self.make_user()

        prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        self.assertIsNone(prediction)
        self.assertFalse(created)

    def test_stale_scheduled_status_past_start_time_is_still_rejected(self):
        """Docstring Match.is_prediction_open() прямо предупреждает про этот
        случай: manual_override-матч может остаться 'scheduled' с
        устаревшей start_time в прошлом (не синхронизирован автосинком) —
        секундная проверка `now < start_time` должна перекрыть дыру, даже
        если статус формально ещё 'scheduled'."""
        match = self.make_match(status='scheduled', start_time=timezone.now() - timedelta(hours=1))
        user = self.make_user()

        prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        self.assertIsNone(prediction)
        self.assertFalse(created)


class ChangePredictionBeforeStartTests(PredictionsTestCase):
    """До старта матча пользователь может переголосовать сколько угодно раз
    — это UPDATE той же строки (update_or_create), не вторая запись (см.
    docstring submit_prediction про отсутствие toggle-off и уникальность
    (match, user))."""

    def test_changing_choice_before_start_updates_same_row_not_creates_new(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()

        first, first_created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)
        second, second_created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_AWAY)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(MatchPrediction.objects.filter(match=match, user=user).count(), 1)
        self.assertEqual(MatchPrediction.objects.get(match=match, user=user).choice, MatchPrediction.CHOICE_AWAY)

    def test_repeating_the_same_choice_is_a_harmless_noop(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()

        submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_DRAW)
        _prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_DRAW)

        self.assertFalse(created)
        self.assertEqual(MatchPrediction.objects.filter(match=match, user=user).count(), 1)


class CannotChangePredictionAfterStartTests(PredictionsTestCase):
    """Как только матч стартовал, прогноз нельзя ни поставить, ни изменить —
    submit_prediction проверяет is_prediction_open() при КАЖДОМ вызове, не
    только при первом (см. docstring submit_prediction про гонку "открыл
    страницу до старта, кликнул уже после")."""

    def test_cannot_change_prediction_after_match_starts(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        # Матч "начался" — статус синхронизировался на 'live'.
        match.status = 'live'
        match.start_time = timezone.now() - timedelta(minutes=1)
        match.save(update_fields=['status', 'start_time'])

        prediction, created = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_AWAY)

        self.assertIsNone(prediction)
        self.assertFalse(created)
        # Исходный прогноз остался нетронутым — попытка смены после старта
        # молча отклонена, а не тихо перезаписала выбор.
        stored = MatchPrediction.objects.get(match=match, user=user)
        self.assertEqual(stored.choice, MatchPrediction.CHOICE_HOME)


class ScoringTests(PredictionsTestCase):
    """MatchPrediction.is_correct — сверка "ваш прогноз vs итог", доступна
    только после того, как у матча есть Match.final_result."""

    def test_is_correct_none_while_match_not_finished(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        prediction, _ = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        self.assertIsNone(prediction.is_correct)

    def test_is_correct_true_when_choice_matches_home_win(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        prediction, _ = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        match.status = 'finished'
        match.home_score, match.away_score = 2, 0
        match.save(update_fields=['status', 'home_score', 'away_score'])

        self.assertTrue(prediction.is_correct)

    def test_is_correct_false_when_choice_does_not_match_result(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        prediction, _ = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)

        match.status = 'finished'
        match.home_score, match.away_score = 0, 1
        match.save(update_fields=['status', 'home_score', 'away_score'])

        self.assertFalse(prediction.is_correct)

    def test_is_correct_true_for_predicted_draw(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        prediction, _ = submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_DRAW)

        match.status = 'finished'
        match.home_score, match.away_score = 1, 1
        match.save(update_fields=['status', 'home_score', 'away_score'])

        self.assertTrue(prediction.is_correct)


class CommunityConsensusTests(PredictionsTestCase):
    """prediction_counts() — "% голосов сообщества за каждый исход",
    показывается ДО матча (в отличие от evaluations, где результаты скрыты
    до конца голосования, см. docstring predictions/models.py)."""

    def test_percentages_reflect_vote_distribution(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        for _ in range(3):
            submit_prediction(user=self.make_user(), match=match, choice=MatchPrediction.CHOICE_HOME)
        submit_prediction(user=self.make_user(), match=match, choice=MatchPrediction.CHOICE_DRAW)

        counts = prediction_counts(match)

        self.assertEqual(counts['home'], 3)
        self.assertEqual(counts['draw'], 1)
        self.assertEqual(counts['away'], 0)
        self.assertEqual(counts['total'], 4)
        self.assertEqual(counts['home_pct'], 75)
        self.assertEqual(counts['draw_pct'], 25)
        self.assertEqual(counts['away_pct'], 0)

    def test_no_votes_returns_zero_percent_without_division_by_zero(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))

        counts = prediction_counts(match)

        self.assertEqual(counts['total'], 0)
        self.assertEqual(counts['home_pct'], 0)
        self.assertEqual(counts['draw_pct'], 0)
        self.assertEqual(counts['away_pct'], 0)

    def test_uneven_split_rounds_without_crashing(self):
        """1/1/1 — классический "33.33% x3" случай, где сумма округлённых
        процентов может не давать ровно 100 (99). Это ожидаемо (round() на
        каждую опцию независимо, см. docstring prediction_counts) — тест
        фиксирует именно это поведение, а не 100%-инвариант по сумме."""
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        submit_prediction(user=self.make_user(), match=match, choice=MatchPrediction.CHOICE_HOME)
        submit_prediction(user=self.make_user(), match=match, choice=MatchPrediction.CHOICE_DRAW)
        submit_prediction(user=self.make_user(), match=match, choice=MatchPrediction.CHOICE_AWAY)

        counts = prediction_counts(match)

        self.assertEqual(counts['total'], 3)
        self.assertEqual(counts['home_pct'], 33)
        self.assertEqual(counts['draw_pct'], 33)
        self.assertEqual(counts['away_pct'], 33)

    def test_changed_prediction_is_counted_once_under_new_choice(self):
        """Переголосование (П1 -> Х) — это UPDATE, счётчики не должны
        задваивать пользователя и должны отражать ТЕКУЩИЙ выбор."""
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_HOME)
        submit_prediction(user=user, match=match, choice=MatchPrediction.CHOICE_DRAW)

        counts = prediction_counts(match)

        self.assertEqual(counts['total'], 1)
        self.assertEqual(counts['home'], 0)
        self.assertEqual(counts['draw'], 1)


class UserPredictionTests(PredictionsTestCase):
    """user_prediction() — прогноз ИМЕННО этого пользователя, для подсветки
    его выбора на карточке."""

    def test_anonymous_user_has_no_prediction(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        self.assertIsNone(user_prediction(AnonymousUser(), match))

    def test_returns_only_this_users_own_prediction(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user_a = self.make_user("alice")
        user_b = self.make_user("bob")
        submit_prediction(user=user_a, match=match, choice=MatchPrediction.CHOICE_HOME)
        submit_prediction(user=user_b, match=match, choice=MatchPrediction.CHOICE_AWAY)

        mine = user_prediction(user_a, match)

        self.assertIsNotNone(mine)
        self.assertEqual(mine.user_id, user_a.id)
        self.assertEqual(mine.choice, MatchPrediction.CHOICE_HOME)

    def test_none_when_this_user_has_not_predicted_yet(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        submit_prediction(user=self.make_user("other"), match=match, choice=MatchPrediction.CHOICE_HOME)

        self.assertIsNone(user_prediction(user, match))


@override_settings(CACHES=LOCMEM_CACHE)
class PredictWidgetViewTests(PredictionsTestCase):
    """View-смоук-тесты для путей, не завязанных на Celery/Redis (см.
    docstring модуля вверху файла про причину не гонять полный
    аутентифицированный POST здесь). CACHES переопределён на LocMemCache —
    predict() зовёт is_rate_limited(), а прод использует RedisCache (см.
    core/tests.py про тот же приём)."""

    def test_anonymous_predict_returns_login_prompt_without_creating_prediction(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))

        response = self.client.post(
            reverse('predictions:predict', args=[match.id]), {'choice': MatchPrediction.CHOICE_HOME},
        )

        # 200, не 401/403 — HTMX по умолчанию свапает контент только на 2xx
        # (см. docstring predict() в views.py).
        self.assertEqual(response.status_code, 200)
        self.assertEqual(MatchPrediction.objects.count(), 0)

    def test_invalid_choice_returns_400(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        user = self.make_user()
        self.client.force_login(user)

        response = self.client.post(reverse('predictions:predict', args=[match.id]), {'choice': 'not-a-choice'})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(MatchPrediction.objects.count(), 0)

    def test_widget_partial_renders_current_counts(self):
        match = self.make_match(status='scheduled', start_time=timezone.now() + timedelta(days=2))
        submit_prediction(user=self.make_user(), match=match, choice=MatchPrediction.CHOICE_HOME)

        response = self.client.get(reverse('predictions:widget', args=[match.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['counts']['home'], 1)
        self.assertEqual(response.context['counts']['total'], 1)
