# evaluations/tests.py
"""Тесты вайзарда оценки: гейт голосования, порядок шагов, XP, антифрод скорости."""
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
# EvaluationSession — прогресс и таймер
# ---------------------------------------------------------------------------

class EvaluationSessionModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
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
# Гейт голосования
# ---------------------------------------------------------------------------

class VotingAccessGateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
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
# Строгий порядок шагов
# ---------------------------------------------------------------------------

class StepOrderGatingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
        self.client.force_login(self.user)
        self.match = _make_match()

    def _session(self, completed_steps):
        return EvaluationSession.objects.create(
            user=self.user, match=self.match, completed_steps=completed_steps, status="in_progress"
        )

    def _assert_redirects_to(self, response, expected_url):
        """Проверяем только код и адрес редиректа — у целевого шага свои гейты."""
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
        """has_lineup=False — шаг игроков не пускает."""
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
# Начисление XP по шагам
# ---------------------------------------------------------------------------

class ContextStepXPTests(TestCase):
    def setUp(self):
        # trust_score=1.25 — множитель XP ровно 1.0.
        self.user = User.objects.create_user(
            username="u1", email="u1@example.com", password="pass123", trust_score=1.25, is_verified=True,
        )
        UserXP.objects.create(user=self.user)
        self.client.force_login(self.user)
        self.match = _make_match()

    def test_context_step_xp_is_pending_until_completion(self):
        url = reverse("evaluations:context", args=[self.match.id])
        response = self.client.post(url, {"watched_type": "full"})
        self.assertRedirects(response, reverse("evaluations:teams", args=[self.match.id]))

        self.user.xp.refresh_from_db()
        self.assertEqual(self.user.xp.total_xp, 0)  # незавершённая оценка XP не даёт
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.pending_xp, XP_CONTEXT_STEP)

    def test_resubmitting_context_step_does_not_double_pending_xp(self):
        """Повторное сохранение шага не добавляет XP второй раз."""
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full"})
        self.client.post(url, {"watched_type": "highlights"})
        self.assertEqual(EvaluationSession.objects.get(user=self.user, match=self.match).pending_xp, XP_CONTEXT_STEP)


# ---------------------------------------------------------------------------
# Режим «Быстро/Подробно»
# ---------------------------------------------------------------------------

class QuickModeSelectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
        self.client.force_login(self.user)
        self.match = _make_match()

    def test_default_mode_is_quick_without_eval_mode_in_post(self):
        """Без eval_mode в POST — дефолтный режим 'quick'."""
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "quick")

    def test_choosing_full_mode_persists_on_session(self):
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full", "eval_mode": "full"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "full")

    def test_choosing_quick_mode_persists_on_session(self):
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full", "eval_mode": "quick"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "quick")

    def test_garbage_eval_mode_value_ignored(self):
        """Мусор в eval_mode игнорируется."""
        url = reverse("evaluations:context", args=[self.match.id])
        self.client.post(url, {"watched_type": "full", "eval_mode": "ultra-mega-mode"})
        session = EvaluationSession.objects.get(user=self.user, match=self.match)
        self.assertEqual(session.mode, "quick")


class KeyPlayerSelectionTests(TestCase):
    """_compute_key_player_ids — ключевые игроки для режима «Быстро»."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
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

    def test_full_mode_has_no_key_player_preselection(self):
        from events.models import MatchEvent

        # Режим 'full' явно — дефолт теперь 'quick'.
        EvaluationSession.objects.create(
            user=self.user, match=self.match, mode="full",
            completed_steps=["context", "teams"],
        )
        home_players = self._make_lineup("home", self.match.home_team, starters=5, bench=2)
        self._make_lineup("away", self.match.away_team, starters=5, bench=2)
        # Гол забил игрок №5 — эвристика «первые по номеру» его бы не выбрала.
        scorer = home_players[4]
        MatchEvent.objects.create(
            match=self.match, event_type="goal", team_side="home",
            player=scorer, minute=23,
        )

        response = self.client.get(reverse("evaluations:players", args=[self.match.id]))
        key_ids = response.context["key_player_ids"]
        # В режиме 'full' key_player_ids пустой.
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
# Пикер «Лучший/худший игрок» — проверяем результат POST с пресетными значениями
# ---------------------------------------------------------------------------

class PlayerBestWorstPickerSubmissionTests(TestCase):
    # Совпадает с BEST_PRESET/WORST_PRESET в players.html.
    BEST_PRESET = {'contribution': 9, 'risk': 2, 'potential': 8}
    WORST_PRESET = {'contribution': 3, 'risk': 8, 'potential': 3}

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
        self.client.force_login(self.user)
        self.match = _make_match(has_lineup=True)
        EvaluationSession.objects.create(
            user=self.user, match=self.match, mode="quick",
            completed_steps=["context", "teams"],
        )

    def _make_lineup(self, side, team, count=3):
        from lineups.models import MatchLineup, MatchLineupPlayer
        from players.models import Player

        lineup = MatchLineup.objects.create(match=self.match, team=team, side=side)
        players = []
        for i in range(count):
            player = Player.objects.create(first_name=f"P{side}", last_name=str(i), team=team)
            MatchLineupPlayer.objects.create(
                lineup=lineup, player=player, is_starting=True, shirt_number=i + 1,
            )
            players.append(player)
        return players

    def _post_data_for(self, player, preset):
        prefix = f'player_{player.id}'
        data = {
            f'{prefix}_evaluate': 'on',
            f'{prefix}_contribution': str(preset['contribution']),
            f'{prefix}_risk': str(preset['risk']),
            f'{prefix}_potential': str(preset['potential']),
            # Без __touched — сохранение пресетов не должно блокироваться.
        }
        return data

    def test_best_and_worst_preset_values_saved_for_exactly_two_players(self):
        from evaluations.models import PlayerEvaluation

        home_players = self._make_lineup("home", self.match.home_team, count=3)
        away_players = self._make_lineup("away", self.match.away_team, count=3)
        best_player = home_players[0]
        worst_player = away_players[0]

        data = {}
        data.update(self._post_data_for(best_player, self.BEST_PRESET))
        data.update(self._post_data_for(worst_player, self.WORST_PRESET))

        response = self.client.post(reverse("evaluations:players", args=[self.match.id]), data)
        self.assertEqual(response.status_code, 302)

        self.assertEqual(PlayerEvaluation.objects.filter(match=self.match).count(), 2)

        best_eval = PlayerEvaluation.objects.get(match=self.match, player=best_player)
        self.assertEqual(best_eval.contribution, self.BEST_PRESET['contribution'])
        self.assertEqual(best_eval.risk, self.BEST_PRESET['risk'])
        self.assertEqual(best_eval.potential, self.BEST_PRESET['potential'])

        worst_eval = PlayerEvaluation.objects.get(match=self.match, player=worst_player)
        self.assertEqual(worst_eval.contribution, self.WORST_PRESET['contribution'])
        self.assertEqual(worst_eval.risk, self.WORST_PRESET['risk'])
        self.assertEqual(worst_eval.potential, self.WORST_PRESET['potential'])

    def test_untouched_players_not_evaluated(self):
        """Остальные игроки не сохраняются."""
        from evaluations.models import PlayerEvaluation

        home_players = self._make_lineup("home", self.match.home_team, count=3)
        self._make_lineup("away", self.match.away_team, count=3)
        best_player = home_players[0]

        data = self._post_data_for(best_player, self.BEST_PRESET)
        self.client.post(reverse("evaluations:players", args=[self.match.id]), data)

        self.assertEqual(PlayerEvaluation.objects.filter(match=self.match).count(), 1)
        self.assertTrue(PlayerEvaluation.objects.filter(match=self.match, player=best_player).exists())


# ---------------------------------------------------------------------------
# Антифрод: слишком быстрое заполнение
# ---------------------------------------------------------------------------

class FastWizardAntiFraudTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
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
# EvaluationPolicy — сущность должна принадлежать матчу
# ---------------------------------------------------------------------------

class ContextFormPolicyTests(TestCase):
    def setUp(self):
        self.match = _make_match()
        self.outside_team = Team.objects.create(name="Сторонняя команда")

    def test_supported_team_outside_match_rejected_even_if_queryset_bypassed(self):
        """Проверяем метод политики напрямую, минуя ModelChoiceField."""
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
        """Через форму: чужую команду отклоняет ModelChoiceField."""
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

    def test_stadium_plus_highlights_normalized_to_full(self):
        """«Стадион» + «только голы» тихо нормализуется."""
        from evaluations.forms import ContextEvaluationForm

        form = ContextEvaluationForm(
            data={
                "supported_team": "",
                "watched_type": "highlights",
                "attended_stadium": True,
            },
            match=self.match,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["watched_type"], "full")

    def test_stadium_plus_partial_left_untouched(self):
        """«Стадион» + «фрагменты» — допустимо."""
        from evaluations.forms import ContextEvaluationForm

        form = ContextEvaluationForm(
            data={
                "supported_team": "",
                "watched_type": "partial",
                "attended_stadium": True,
            },
            match=self.match,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["watched_type"], "partial")


# ---------------------------------------------------------------------------
# Анти-шум ползунков: нетронутый критерий не сохраняется
# ---------------------------------------------------------------------------

class AntiNoiseTouchedTrackingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=True)
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
        """Есть __touched, но все '0' — ничего не сохраняется."""
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
        """Тронута только домашняя — сохраняется только она."""
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
        """Нет __touched вообще (JS не сработал) — сохраняются обе."""
        from evaluations.models import TeamEvaluation

        data = self._team_post_data()
        self.client.post(reverse("evaluations:teams", args=[self.match.id]), data)
        self.assertEqual(TeamEvaluation.objects.filter(user=self.user, match=self.match).count(), 2)
