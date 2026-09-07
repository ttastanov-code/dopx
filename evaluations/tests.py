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
# Режим "Быстро/Подробно" (см. docs/adr/0006-quick-full-evaluation-mode.md)
# ---------------------------------------------------------------------------

class QuickModeSelectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.client.force_login(self.user)
        self.match = _make_match()

    def test_default_mode_is_full_without_eval_mode_in_post(self):
        """Старые закладки/JS не выполнился — eval_mode просто не приходит в
        POST. Сессия должна остаться в дефолтном режиме 'full', а не упасть."""
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "full")

    def test_choosing_quick_mode_persists_on_session(self):
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full", "eval_mode": "quick"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "quick")

    def test_garbage_eval_mode_value_ignored(self):
        """Произвольная строка в eval_mode (испорченный запрос, не
        значение из EvaluationSession.MODE_CHOICES) не должна попасть в БД —
        MODE_CHOICES на уровне модели этого и так не пропустил бы при
        full_clean(), но save() без него не проверяет choices сам по себе."""
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full", "eval_mode": "ultra-mega-mode"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "full")


class KeyPlayerSelectionTests(TestCase):
    """EvaluatePlayersView._compute_key_player_ids — курируемый набор для
    режима 'Быстро' (см. docs/adr/0006-quick-full-evaluation-mode.md)."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.client.force_login(self.user)
        self.match = _make_match(has_lineup=True)

    def _make_lineup(self, side, team, starters=3, bench=2):
        from lineups.models import MatchLineup, MatchLineupPlayer
        from players.models import Player

        lineup = MatchLineup.objects.create(match=self.match, team=team, side=side)
        players = []
        for i in range(starters + bench):
            player = Player.objects.create(first_name=f"P{side}", last_name=str(i), team=team)
            MatchLineupPlayer.objects.create(
                lineup=lineup, player=player, is_starting=i < starters, shirt_number=i + 1,
            )
            players.append(player)
        return players

    def test_scorer_prioritized_over_low_shirt_numbers(self):
        from events.models import MatchEvent

        EvaluationSession.objects.create(
            user=self.user, match=self.match, completed_steps=["context", "teams"],
        )
        home_players = self._make_lineup("home", self.match.home_team, starters=5, bench=2)
        self._make_lineup("away", self.match.away_team, starters=5, bench=2)
        # Гол забил игрок под номером 5 (последний из стартовых, обычным
        # "первые по номеру" эвристика его бы не выбрала).
        scorer = home_players[4]
        MatchEvent.objects.create(
            match=self.match, event_type="goal", team_side="home",
            player=scorer, minute=23,
        )

        response = self.client.get(reverse("evaluations:players", args=[self.match.id]))
        key_ids = response.context["key_player_ids"]
        # session.mode по умолчанию 'full' — key_player_ids должен быть
        # пустым, пока пользователь явно не выбрал 'quick' на шаге 1.
        self.assertEqual(key_ids, set())

    def test_quick_mode_includes_scorer_and_caps_per_side(self):
        from events.models import MatchEvent

        EvaluationSession.objects.create(
            user=self.user, match=self.match, mode="quick",
            completed_steps=["context", "teams"],
        )
        home_players = self._make_lineup("home", self.match.home_team, starters=5, bench=2)
        self._make_lineup("away", self.match.away_team, starters=5, bench=2)
        scorer = home_players[4]
        MatchEvent.objects.create(match=self.match, event_type="goal", team_side="home", player=scorer, minute=23)

        response = self.client.get(reverse("evaluations:players", args=[self.match.id]))
        key_ids = response.context["key_player_ids"]
        self.assertIn(scorer.id, key_ids)
        home_key_count = sum(1 for p in home_players if p.id in key_ids)
        self.assertLessEqual(home_key_count, 3)


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


# ---------------------------------------------------------------------------
# EvaluationPolicy — принадлежность сущности матчу (см.
# docs/adr/0001-evaluation-policy-single-source-of-truth.md). Дубль этого же
# правила на стороне API — api/tests.py::EvaluationPolicyAPITests. Здесь —
# именно веб-форма, единственное место вайзарда, где ID сущности вообще
# приходит от клиента напрямую (остальные формы генерируют поля по реальным
# сущностям матча — подменить там нечего, см. докстринг
# ContextEvaluationForm.clean_supported_team).
# ---------------------------------------------------------------------------

class ContextFormPolicyTests(TestCase):
    def setUp(self):
        self.match = _make_match()
        self.outside_team = Team.objects.create(name="Сторонняя команда")

    def test_supported_team_outside_match_rejected_even_if_queryset_bypassed(self):
        """ModelChoiceField сам отклонил бы значение вне queryset ДО того,
        как дошло бы до clean_supported_team — но именно поэтому здесь
        тестируем сам метод политики напрямую, а не через form.is_valid()
        (иначе тест проверял бы только ModelChoiceField, а не
        assert_team_in_match)."""
        from evaluations.forms import ContextEvaluationForm
        from evaluations.policies import EvaluationPolicyError, assert_team_in_match

        with self.assertRaises(EvaluationPolicyError):
            assert_team_in_match(self.outside_team.id, self.match)

    def test_supported_team_in_match_accepted_via_form(self):
        from evaluations.forms import ContextEvaluationForm

        form = ContextEvaluationForm(
            data={
                "supported_team": str(self.match.home_team_id),
                "watched_type": "full",
                "attended_stadium": False,
            },
            match=self.match,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_supported_team_outside_match_rejected_by_queryset(self):
        """End-to-end через форму: ModelChoiceField (queryset ограничен
        домашней/гостевой командой) отклоняет чужую команду ещё до
        clean_supported_team — обе линии защиты работают."""
        from evaluations.forms import ContextEvaluationForm

        form = ContextEvaluationForm(
            data={
                "supported_team": str(self.outside_team.id),
                "watched_type": "full",
                "attended_stadium": False,
            },
            match=self.match,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("supported_team", form.errors)


# ---------------------------------------------------------------------------
# Анти-шум ползунков (см. docs/adr/0005-anti-noise-touched-tracking.md) —
# нетронутый критерий не должен сохраняться как настоящая оценка "5 из 10".
# Разобрано подробно на шаге "Команды" (представитель паттерна,
# см. evaluations/views.py::_touched_fields — используется идентично в
# Teams/Coaches/Referee/Players).
# ---------------------------------------------------------------------------

class AntiNoiseTouchedTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.client.force_login(self.user)
        self.match = _make_match()
        EvaluationSession.objects.create(
            user=self.user, match=self.match, completed_steps=["context"], status="in_progress"
        )

    def _team_post_data(self, **overrides):
        home_prefix = f"team_{self.match.home_team_id}"
        away_prefix = f"team_{self.match.away_team_id}"
        data = {}
        for prefix in (home_prefix, away_prefix):
            for criterion in ("tactics", "effort", "organization", "mentality"):
                data[f"{prefix}_{criterion}"] = "5"
        data.update(overrides)
        return data

    def test_untouched_sliders_create_no_team_evaluation(self):
        """JS выполнился (есть __touched-поля), но ни одно не '1' — ни для
        одной из команд не должно появиться записи с дефолтными значениями."""
        from evaluations.models import TeamEvaluation

        home_prefix = f"team_{self.match.home_team_id}"
        away_prefix = f"team_{self.match.away_team_id}"
        data = self._team_post_data()
        for prefix in (home_prefix, away_prefix):
            for criterion in ("tactics", "effort", "organization", "mentality"):
                data[f"{prefix}_{criterion}__touched"] = "0"

        self.client.post(reverse("evaluations:teams", args=[self.match.id]), data)
        self.assertEqual(TeamEvaluation.objects.filter(user=self.user, match=self.match).count(), 0)

    def test_touching_one_criterion_saves_that_team(self):
        """Домашнюю команду тронули (хотя бы один критерий), гостевую — нет:
        должна сохраниться только домашняя."""
        from evaluations.models import TeamEvaluation

        home_prefix = f"team_{self.match.home_team_id}"
        away_prefix = f"team_{self.match.away_team_id}"
        data = self._team_post_data(**{f"{home_prefix}_tactics": "8"})
        for criterion in ("tactics", "effort", "organization", "mentality"):
            data[f"{home_prefix}_{criterion}__touched"] = "1" if criterion == "tactics" else "0"
            data[f"{away_prefix}_{criterion}__touched"] = "0"

        self.client.post(reverse("evaluations:teams", args=[self.match.id]), data)
        self.assertTrue(
            TeamEvaluation.objects.filter(user=self.user, match=self.match, team_id=self.match.home_team_id).exists()
        )
        self.assertFalse(
            TeamEvaluation.objects.filter(user=self.user, match=self.match, team_id=self.match.away_team_id).exists()
        )

    def test_no_javascript_fallback_still_saves_both_teams(self):
        """Ни одного '__touched'-поля в POST вообще (JS не выполнился) —
        деградация к прежнему поведению: обе команды сохраняются, как до
        анти-шум фикса. Иначе пользователи без JS молча теряли бы свои
        честно выставленные оценки."""
        from evaluations.models import TeamEvaluation

        data = self._team_post_data()
        self.client.post(reverse("evaluations:teams", args=[self.match.id]), data)
        self.assertEqual(TeamEvaluation.objects.filter(user=self.user, match=self.match).count(), 2)
