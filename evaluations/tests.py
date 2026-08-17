# evaluations/tests.py
"""
Регрессионные тесты вайзарда оценки матча — самой часто правимой и самой
"денежной" для продукта логики (voting_open-гейт, порядок шагов, начисление
XP, антифрод-флаг скорости заполнения). До этой сессии здесь не было ни
одного теста, при этом сама эта сессия несколько раз показала, насколько
тонкие баги здесь возможны (см. users/tests.py докстринг).

Полные end-to-end прохождения всех 6 шагов через HTTP (с реальными составами
игроков/тренеров) сюда намеренно не включены — это отдельная, кратно более
тяжёлая инфраструктура (MatchLineup/MatchLineupPlayer/Coach), которая скорее
поле для интеграционных/E2E тестов, чем для юнит-регрессии. Здесь — то, что
реально ломалось или могло сломаться незаметно: гейт голосования, СТРОГИЙ
порядок прохождения шагов (нельзя перепрыгнуть), компонентное начисление XP
и идемпотентность повторного прохождения уже пройденного шага.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from evaluations.models import EvaluationSession
from evaluations.views import XP_CONTEXT_STEP
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team
from users.models import SuspiciousActivityFlag, UserXP
from users.tasks import MIN_HUMAN_WIZARD_SECONDS, flag_suspicious_wizard_speed_task

User = get_user_model()


def _make_match(status="finished", voting_open_until=None, has_lineup=False):
    league = League.objects.create(name=f"League-{League.objects.count()}", country="KZ")
    season, _created = Season.objects.get_or_create(league=league, year="2026")
    home = Team.objects.create(name=f"Home-{Team.objects.count()}")
    away = Team.objects.create(name=f"Away-{Team.objects.count()}")
    return Match.objects.create(
        league=league, season=season, home_team=home, away_team=away,
        start_time=timezone.now() - timedelta(hours=2),
        end_time=timezone.now(),
        status=status,
        voting_open_until=voting_open_until or (timezone.now() + timedelta(hours=48)),
        has_lineup=has_lineup,
    )


# ---------------------------------------------------------------------------
# EvaluationSession — прогресс и антифрод-таймер
# ---------------------------------------------------------------------------

class EvaluationSessionModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.match = _make_match()

    def test_progress_percentage_zero_with_no_steps(self):
        session = EvaluationSession.objects.create(user=self.user, match=self.match)
        self.assertEqual(session.progress_percentage(), 0)

    def test_progress_percentage_scales_with_completed_steps(self):
        session = EvaluationSession.objects.create(
            user=self.user, match=self.match, completed_steps=["context", "teams", "players"]
        )
        self.assertEqual(session.progress_percentage(), 50)  # 3 из 6 шагов

    def test_fill_duration_seconds_none_while_not_completed(self):
        session = EvaluationSession.objects.create(user=self.user, match=self.match)
        self.assertIsNone(session.fill_duration_seconds)

    def test_fill_duration_seconds_computed_after_completion(self):
        session = EvaluationSession.objects.create(user=self.user, match=self.match)
        session.completed_at = session.started_at + timedelta(seconds=42)
        session.save(update_fields=["completed_at"])
        self.assertAlmostEqual(session.fill_duration_seconds, 42, delta=0.5)


# ---------------------------------------------------------------------------
# Гейт голосования — единственная точка входа во весь вайзард
# ---------------------------------------------------------------------------

class VotingAccessGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.client.force_login(self.user)

    def test_voting_closed_redirects_to_match_detail(self):
        match = _make_match(voting_open_until=timezone.now() - timedelta(hours=1))
        response = self.client.get(reverse("evaluations:context", args=[match.id]))
        self.assertRedirects(response, reverse("matches:detail", kwargs={"pk": match.id}))

    def test_match_not_finished_redirects_to_match_detail(self):
        match = _make_match(status="live")
        response = self.client.get(reverse("evaluations:context", args=[match.id]))
        self.assertRedirects(response, reverse("matches:detail", kwargs={"pk": match.id}))

    def test_open_and_finished_match_renders_wizard(self):
        match = _make_match()
        response = self.client.get(reverse("evaluations:context", args=[match.id]))
        self.assertEqual(response.status_code, 200)

    def test_already_completed_evaluation_redirects_away(self):
        match = _make_match()
        EvaluationSession.objects.create(user=self.user, match=match, status="completed")
        response = self.client.get(reverse("evaluations:context", args=[match.id]))
        self.assertRedirects(response, reverse("matches:detail", kwargs={"pk": match.id}))


# ---------------------------------------------------------------------------
# Строгий порядок прохождения шагов — нельзя перепрыгнуть вперёд
# ---------------------------------------------------------------------------

class StepOrderGatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.client.force_login(self.user)
        self.match = _make_match()

    def _session(self, completed_steps):
        return EvaluationSession.objects.create(
            user=self.user, match=self.match, completed_steps=completed_steps, status="in_progress"
        )

    def _assert_redirects_to(self, response, expected_url):
        """
        Прямая проверка (status_code + .url) вместо assertRedirects().

        assertRedirects() по умолчанию САМ делает GET по целевому URL и
        требует от него 200 — но целевые шаги вайзарда (players/teams/...)
        имеют СВОИ гейты (см. test_coaches_blocked_without_players: coaches
        корректно редиректит на players, но players САМ редиректит дальше
        на matches:detail, т.к. has_lineup=False по умолчанию в
        _make_match()). Тест здесь проверяет ТОЛЬКО факт и адрес редиректа
        с текущего шага — не поведение шага, на который редиректнули (оно
        покрыто отдельными тестами).
        """
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, expected_url)

    def test_teams_blocked_without_context(self):
        self._session([])
        response = self.client.get(reverse("evaluations:teams", args=[self.match.id]))
        self._assert_redirects_to(response, reverse("evaluations:context", args=[self.match.id]))

    def test_teams_allowed_after_context(self):
        self._session(["context"])
        response = self.client.get(reverse("evaluations:teams", args=[self.match.id]))
        self.assertEqual(response.status_code, 200)

    def test_players_blocked_without_teams(self):
        self._session(["context"])
        response = self.client.get(reverse("evaluations:players", args=[self.match.id]))
        self._assert_redirects_to(response, reverse("evaluations:teams", args=[self.match.id]))

    def test_players_blocked_without_lineup_even_with_teams_done(self):
        """has_lineup=False (см. _make_match по умолчанию) — шаг не пускает,
        даже если шаг 'teams' уже пройден: иначе можно формально "пройти"
        шаг игроков с пустым составом и получить полный XP (см. докстринг
        EvaluatePlayersView в evaluations/views.py)."""
        self._session(["context", "teams"])
        response = self.client.get(reverse("evaluations:players", args=[self.match.id]))
        self._assert_redirects_to(response, reverse("matches:detail", kwargs={"pk": self.match.id}))

    def test_players_allowed_with_teams_done_and_lineup_present(self):
        self.match.has_lineup = True
        self.match.save(update_fields=["has_lineup"])
        self._session(["context", "teams"])
        response = self.client.get(reverse("evaluations:players", args=[self.match.id]))
        self.assertEqual(response.status_code, 200)

    def test_coaches_blocked_without_players(self):
        self._session(["context", "teams"])
        response = self.client.get(reverse("evaluations:coaches", args=[self.match.id]))
        self._assert_redirects_to(response, reverse("evaluations:players", args=[self.match.id]))

    def test_referee_blocked_without_coaches(self):
        self._session(["context", "teams", "players"])
        response = self.client.get(reverse("evaluations:referee", args=[self.match.id]))
        self._assert_redirects_to(response, reverse("evaluations:coaches", args=[self.match.id]))

    def test_final_blocked_without_referee(self):
        self._session(["context", "teams", "players", "coaches"])
        response = self.client.get(reverse("evaluations:match_eval", args=[self.match.id]))
        self._assert_redirects_to(response, reverse("evaluations:referee", args=[self.match.id]))

    def test_final_allowed_after_all_prior_steps_done(self):
        self._session(["context", "teams", "players", "coaches", "referee"])
        response = self.client.get(reverse("evaluations:match_eval", args=[self.match.id]))
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# XP-начисление по шагам — компонентное, не фиксированное
# ---------------------------------------------------------------------------

class ContextStepXPTests(TestCase):
    def setUp(self):
        # trust_score=1.25 — ровно середина диапазона [0.5, 2.0] у
        # xp_multiplier(), множитель == 1.0 (см. users/tests.py::
        # UserXpMultiplierAndTrustLevelTests) — убирает multiplier как
        # переменную и делает ожидаемое значение точным.
        self.user = User.objects.create_user(
            username="u1", email="u1@example.com", password="pass123", trust_score=1.25
        )
        UserXP.objects.create(user=self.user)
        self.client.force_login(self.user)
        self.match = _make_match()

    def test_completing_context_step_awards_xp(self):
        url = reverse("evaluations:context", args=[self.match.id])
        response = self.client.post(url, {"watched_type": "full"})
        self.assertRedirects(response, reverse("evaluations:teams", args=[self.match.id]))

        self.user.xp.refresh_from_db()
        self.assertEqual(self.user.xp.total_xp, XP_CONTEXT_STEP)

    def test_resubmitting_context_step_does_not_double_award_xp(self):
        """is_new_step в EvaluateContextView.form_valid проверяет 'context'
        не в completed_steps ДО апдейта сессии — повторное сохранение того
        же шага (например, пользователь вернулся назад и поменял ответ)
        не должно начислять XP второй раз."""
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full"})
        self.client.post(url, {"watched_type": "highlights"})

        self.user.xp.refresh_from_db()
        self.assertEqual(
            self.user.xp.total_xp, XP_CONTEXT_STEP,
            "повторное прохождение уже пройденного шага начислило XP снова",
        )


# ---------------------------------------------------------------------------
# Антифрод: слишком быстрое заполнение вайзарда
# ---------------------------------------------------------------------------

class FastWizardAntiFraudTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.match = _make_match()

    def _completed_session(self, duration_seconds):
        session = EvaluationSession.objects.create(user=self.user, match=self.match, status="completed")
        session.completed_at = session.started_at + timedelta(seconds=duration_seconds)
        session.save(update_fields=["completed_at"])
        return session

    def test_fast_completion_creates_suspicious_flag(self):
        session = self._completed_session(MIN_HUMAN_WIZARD_SECONDS / 4)
        result = flag_suspicious_wizard_speed_task(str(session.id))
        self.assertTrue(result)
        flag = SuspiciousActivityFlag.objects.get(user=self.user, source="fast_wizard")
        self.assertGreater(flag.score, 0)
        self.assertEqual(flag.match_id, self.match.id)

    def test_normal_speed_completion_not_flagged(self):
        session = self._completed_session(MIN_HUMAN_WIZARD_SECONDS * 6)
        result = flag_suspicious_wizard_speed_task(str(session.id))
        self.assertFalse(result)
        self.assertFalse(SuspiciousActivityFlag.objects.filter(user=self.user, source="fast_wizard").exists())

    def test_incomplete_session_not_flagged(self):
        session = EvaluationSession.objects.create(user=self.user, match=self.match, status="in_progress")
        result = flag_suspicious_wizard_speed_task(str(session.id))
        self.assertFalse(result)
        self.assertFalse(SuspiciousActivityFlag.objects.filter(user=self.user, source="fast_wizard").exists())
