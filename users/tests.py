# users/tests.py
"""
Регрессионные тесты ядра продукта: возрастающая кривая XP/уровней, выдача
достижений, анти-фрод барьеры регистрации (honeypot/time-trap), валидация
аватарки (task #132, эта сессия) и rate-limit на password-reset/verify-email/
toggle_follow (task #133, эта сессия).

ПОЧЕМУ ИМЕННО ЭТО: users — самое часто правимое ядро продукта (trust_score,
XP, бейджи, антифрод), и за эту сессию именно в нём нашлось больше всего
тонких багов (StaffSessionSecurityMiddleware, CASCADE на реакциях, ValueError
на verify-email) — без регрессионного щита каждая следующая правка идёт
вслепую. is_rate_limited сама по себе покрыта отдельно в core/tests.py —
здесь только интеграционные тесты того, что 4 новых эндпоинта реально её
вызывают с правильным ключом/лимитом.

CACHES переопределён на LocMemCache во всех тестах, трогающих is_rate_limited
— прод использует Redis (dopx/settings.py), тесты не должны зависеть от того,
поднят ли Redis на машине, где запускается `manage.py test`.
"""
from __future__ import annotations

import time
from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from evaluations.models import PlayerEvaluation, RefereeEvaluation
from leagues.models import League
from players.models import Player
from seasons.models import Season
from teams.models import Team
from matches.models import Match
from users.forms import MIN_FORM_FILL_SECONDS, UserProfileForm, UserRegistrationForm
from users.models import (
    UserBadge, UserXP, cumulative_xp_for_level, level_for_total_xp,
)
from users.services import check_and_award_badges

User = get_user_model()

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-users-rate-limiter",
    }
}


def _make_match(league=None, home=None, away=None):
    league = league or League.objects.create(name="Test League", country="KZ")
    # get_or_create, не create() — Season имеет UniqueConstraint(league, year)
    # (seasons/models.py::unique_league_season). Тесты вроде judge_of_judges
    # вызывают _make_match() в цикле с одной и той же лигой — create() падал
    # бы IntegrityError уже на второй итерации.
    season, _created = Season.objects.get_or_create(league=league, year="2026")
    home = home or Team.objects.create(name="Home")
    away = away or Team.objects.create(name="Away")
    return Match.objects.create(
        league=league, season=season, home_team=home, away_team=away,
        start_time=timezone.now(), voting_open_until=timezone.now() + timedelta(hours=48),
    )


def _make_player(team=None):
    team = team or Team.objects.create(name=f"Team-{Team.objects.count()}")
    return Player.objects.create(first_name="First", last_name="Last", team=team)


# ---------------------------------------------------------------------------
# Кривая уровней/XP
# ---------------------------------------------------------------------------

class LevelCurveTests(TestCase):
    """cumulative_xp_for_level / level_for_total_xp — чистые функции, но
    именно на них построено ВСЁ начисление опыта (UserXP.add_xp)."""

    def test_cumulative_xp_matches_documented_curve(self):
        # Докстринг models.py: 2 уровень — 100 XP, 3 — 300, 4 — 600, 5 — 1000.
        self.assertEqual(cumulative_xp_for_level(1), 0)
        self.assertEqual(cumulative_xp_for_level(2), 100)
        self.assertEqual(cumulative_xp_for_level(3), 300)
        self.assertEqual(cumulative_xp_for_level(4), 600)
        self.assertEqual(cumulative_xp_for_level(5), 1000)

    def test_level_for_total_xp_below_first_threshold(self):
        self.assertEqual(level_for_total_xp(0), 1)
        self.assertEqual(level_for_total_xp(99), 1)

    def test_level_for_total_xp_exact_boundary_rounds_up(self):
        """Ровно на пороге уровня — уже НОВЫЙ уровень (`<=` в cumulative
        сравнении), не старый."""
        self.assertEqual(level_for_total_xp(100), 2)
        self.assertEqual(level_for_total_xp(300), 3)

    def test_level_for_total_xp_just_below_boundary_stays_previous(self):
        self.assertEqual(level_for_total_xp(299), 2)
        self.assertEqual(level_for_total_xp(599), 3)

    def test_round_trip_stable_for_first_30_levels(self):
        """Каждый уровень: XP ровно на его пороге должен репортить именно
        этот уровень — ловит погрешность float в math.sqrt (см. докстринг
        level_for_total_xp про IEEE 754 на границе)."""
        for level in range(1, 31):
            threshold = cumulative_xp_for_level(level)
            self.assertEqual(
                level_for_total_xp(threshold), level,
                f"XP={threshold} (порог уровня {level}) вернул не тот уровень",
            )


class UserXPAddXPTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.xp = UserXP.objects.create(user=self.user)

    def test_add_xp_within_same_level_does_not_level_up(self):
        result = self.xp.add_xp(50)
        self.assertEqual(self.xp.total_xp, 50)
        self.assertEqual(self.xp.level, 1)
        self.assertFalse(result["level_increased"])
        self.assertEqual(result["levels_gained"], [])

    def test_add_xp_crossing_one_level_boundary(self):
        result = self.xp.add_xp(100)  # ровно порог 2 уровня
        self.assertEqual(self.xp.level, 2)
        self.assertTrue(result["level_increased"])
        self.assertEqual(result["levels_gained"], [2])

    def test_add_xp_crossing_multiple_levels_at_once(self):
        """Большой разовый прирост (например, бонус) должен корректно
        перечислить ВСЕ пройденные уровни, не только конечный."""
        result = self.xp.add_xp(650)  # порог 4 уровня — 600
        self.assertEqual(self.xp.level, 4)
        self.assertEqual(result["levels_gained"], [2, 3, 4])

    def test_add_xp_never_goes_negative(self):
        self.xp.add_xp(20)
        self.xp.add_xp(-1000)
        self.assertEqual(self.xp.total_xp, 0)
        self.assertEqual(self.xp.level, 1)

    def test_progress_percent_zero_at_level_start(self):
        self.xp.add_xp(100)  # ровно порог 2 уровня — 0% прогресса ВНУТРИ уровня 2
        self.assertEqual(self.xp.progress_percent, 0)

    def test_progress_percent_full_just_before_next_level(self):
        self.xp.add_xp(299)  # уровень 2, почти вплотную к порогу уровня 3 (300)
        self.assertGreaterEqual(self.xp.progress_percent, 90)
        self.assertLess(self.xp.progress_percent, 100)


class UserXpMultiplierAndTrustLevelTests(TestCase):
    """Чистые вычисления над `trust_score` — не требуют сохранения в БД."""

    def test_xp_multiplier_at_floor(self):
        self.assertEqual(User(trust_score=0.5).xp_multiplier(), 0.8)

    def test_xp_multiplier_at_ceiling(self):
        self.assertEqual(User(trust_score=2.0).xp_multiplier(), 1.2)

    def test_xp_multiplier_midpoint(self):
        self.assertEqual(User(trust_score=1.25).xp_multiplier(), 1.0)

    def test_xp_multiplier_clamps_below_floor(self):
        """trust_score в проекте всегда в [0.5, 2.0], но на вход может
        прийти что угодно (баг в другом месте) — clamp должен спасти."""
        self.assertEqual(User(trust_score=0.1).xp_multiplier(), User(trust_score=0.5).xp_multiplier())

    def test_xp_multiplier_clamps_above_ceiling(self):
        self.assertEqual(User(trust_score=5.0).xp_multiplier(), User(trust_score=2.0).xp_multiplier())

    def test_trust_level_thresholds(self):
        self.assertEqual(User(trust_score=1.8).get_trust_level()[0], "expert")
        self.assertEqual(User(trust_score=1.79).get_trust_level()[0], "reliable")
        self.assertEqual(User(trust_score=1.4).get_trust_level()[0], "reliable")
        self.assertEqual(User(trust_score=1.39).get_trust_level()[0], "standard")
        self.assertEqual(User(trust_score=1.0).get_trust_level()[0], "standard")
        self.assertEqual(User(trust_score=0.99).get_trust_level()[0], "new")


# ---------------------------------------------------------------------------
# Достижения
# ---------------------------------------------------------------------------

class CheckAndAwardBadgesCountThresholdTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")

    def _award(self):
        return {b.badge_type for b in check_and_award_badges(self.user)}

    def test_no_evaluations_no_badges(self):
        self.user.total_evaluations = 0
        self.assertEqual(self._award(), set())

    def test_first_evaluation_badge(self):
        self.user.total_evaluations = 1
        self.assertIn("first_evaluation", self._award())

    def test_active_fan_thresholds(self):
        self.user.total_evaluations = 50
        awarded = self._award()
        self.assertIn("active_fan_10", awarded)
        self.assertIn("active_fan_50", awarded)
        self.assertNotIn("active_fan_150", awarded)

    def test_streak_badges(self):
        self.user.evaluation_streak = 30
        awarded = self._award()
        self.assertIn("streak_7", awarded)
        self.assertIn("streak_30", awarded)
        self.assertNotIn("streak_100", awarded)

    def test_streak_just_below_threshold_not_awarded(self):
        self.user.evaluation_streak = 6
        self.assertNotIn("streak_7", self._award())

    def test_idempotent_second_call_returns_no_new_badges(self):
        """get_or_create внутри check_and_award_badges — повторный вызов с
        тем же состоянием не должен ни падать на UniqueConstraint, ни
        возвращать уже выданные бейджи повторно."""
        self.user.total_evaluations = 10
        first_call = self._award()
        self.assertIn("first_evaluation", first_call)
        second_call = self._award()
        self.assertEqual(second_call, set(), "повторный вызов не должен возвращать уже выданные бейджи")
        # И не должно быть дублей в БД (UniqueConstraint отловил бы это как IntegrityError раньше).
        self.assertEqual(UserBadge.objects.filter(user=self.user, badge_type="first_evaluation").count(), 1)

    def test_foresight_requires_both_volume_and_trust(self):
        self.user.total_evaluations = 30
        self.user.trust_score = 1.6
        self.assertIn("foresight", self._award())

    def test_foresight_not_awarded_with_high_trust_but_low_volume(self):
        self.user.total_evaluations = 5
        self.user.trust_score = 2.0
        self.assertNotIn("foresight", self._award())


class CheckAndAwardBadgesRelatedModelTests(TestCase):
    """judge_of_judges/polyglot требуют реальных PlayerEvaluation/
    RefereeEvaluation — количество различных матчей/команд, не просто
    счётчик на User."""

    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.league = League.objects.create(name="L", country="KZ")

    def test_judge_of_judges_awarded_at_25_referee_evaluations(self):
        self.user.total_evaluations = 25  # разблокирует ветку judge_of_judges в check_and_award_badges
        for _ in range(25):
            match = _make_match(league=self.league)
            RefereeEvaluation.objects.create(user=self.user, match=match, influence_score=50, decision_quality=5)
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertIn("judge_of_judges", awarded)

    def test_judge_of_judges_not_awarded_below_threshold(self):
        self.user.total_evaluations = 25
        for _ in range(24):
            match = _make_match(league=self.league)
            RefereeEvaluation.objects.create(user=self.user, match=match, influence_score=50, decision_quality=5)
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertNotIn("judge_of_judges", awarded)

    def test_polyglot_awarded_across_8_distinct_teams(self):
        self.user.total_evaluations = 10
        match = _make_match(league=self.league)
        for _ in range(8):
            team = Team.objects.create(name=f"Team-{Team.objects.count()}")
            player = _make_player(team=team)
            PlayerEvaluation.objects.create(
                user=self.user, match=match, player=player, contribution=5, risk=5, potential=5
            )
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertIn("polyglot", awarded)

    def test_polyglot_not_awarded_below_8_teams(self):
        self.user.total_evaluations = 10
        match = _make_match(league=self.league)
        for _ in range(7):
            team = Team.objects.create(name=f"Team-{Team.objects.count()}")
            player = _make_player(team=team)
            PlayerEvaluation.objects.create(
                user=self.user, match=match, player=player, contribution=5, risk=5, potential=5
            )
        awarded = {b.badge_type for b in check_and_award_badges(self.user)}
        self.assertNotIn("polyglot", awarded)


# ---------------------------------------------------------------------------
# Анти-фрод регистрации: honeypot + time-trap
# ---------------------------------------------------------------------------

class RegistrationAntiFraudFormTests(TestCase):
    """Тестируются clean_website()/clean_form_rendered_at() напрямую (минуя
    is_valid()), т.к. captcha-поле формы требует реального ответа с картинки
    — не имеет отношения к проверяемой здесь антибот-логике."""

    def test_honeypot_empty_passes(self):
        form = UserRegistrationForm()
        form.cleaned_data = {"website": ""}
        self.assertEqual(form.clean_website(), "")

    def test_honeypot_filled_rejected(self):
        form = UserRegistrationForm()
        form.cleaned_data = {"website": "http://spam.example.com"}
        with self.assertRaises(ValidationError):
            form.clean_website()

    def test_time_trap_instant_submit_rejected(self):
        form = UserRegistrationForm()
        form.cleaned_data = {"form_rendered_at": time.time()}
        with self.assertRaises(ValidationError):
            form.clean_form_rendered_at()

    def test_time_trap_after_min_fill_seconds_passes(self):
        form = UserRegistrationForm()
        rendered_at = time.time() - MIN_FORM_FILL_SECONDS - 1
        form.cleaned_data = {"form_rendered_at": rendered_at}
        self.assertEqual(form.clean_form_rendered_at(), rendered_at)


# ---------------------------------------------------------------------------
# Валидация аватарки (task #132)
# ---------------------------------------------------------------------------

def _valid_png_upload(name="avatar.png"):
    buf = BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/png")


class AvatarValidationFormTests(TestCase):
    def test_valid_png_passes(self):
        form = UserProfileForm()
        upload = _valid_png_upload()
        form.cleaned_data = {"avatar": upload}
        result = form.clean_avatar()
        self.assertIs(result, upload)

    def test_corrupt_file_rejected(self):
        form = UserProfileForm()
        fake = SimpleUploadedFile("avatar.jpg", b"this is not an image, just plain bytes", content_type="image/jpeg")
        form.cleaned_data = {"avatar": fake}
        with self.assertRaises(ValidationError):
            form.clean_avatar()

    @mock.patch("users.forms.MAX_AVATAR_SIZE_BYTES", 10)
    def test_oversized_file_rejected(self):
        """MAX_AVATAR_SIZE_BYTES патчится на 10 байт, чтобы не гонять
        реальные 5МБ+ в памяти теста ради проверки одной ветки."""
        form = UserProfileForm()
        upload = _valid_png_upload()  # заведомо больше 10 байт
        form.cleaned_data = {"avatar": upload}
        with self.assertRaises(ValidationError):
            form.clean_avatar()

    def test_untouched_existing_avatar_not_revalidated(self):
        """Если пользователь не трогал поле avatar — cleaned_data содержит
        уже сохранённый ImageFieldFile (не UploadedFile), и его не нужно
        (и физически нельзя, т.к. файла на диске тестового окружения нет)
        повторно прогонять через Pillow.verify()."""
        user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        user.avatar.name = "avatars/existing.png"
        form = UserProfileForm(instance=user)
        form.cleaned_data = {"avatar": user.avatar}
        result = form.clean_avatar()
        self.assertEqual(result, user.avatar)


# ---------------------------------------------------------------------------
# Rate-limit (task #133): password-reset, verify-email, toggle_follow
# ---------------------------------------------------------------------------

@override_settings(CACHES=LOCMEM_CACHES)
class PasswordResetRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        from users.views import PASSWORD_RESET_RATE_LIMIT
        self.limit = PASSWORD_RESET_RATE_LIMIT

    def test_exceeding_limit_redirects_with_error_instead_of_processing_form(self):
        url = reverse("users:password_reset")
        for _ in range(self.limit):
            self.client.post(url, {"email": "someone@example.com"})

        response = self.client.post(url, {"email": "flood@example.com"})
        self.assertRedirects(response, url)

        # Второй заблокированный запрос, но уже с follow=True — сообщение
        # об ошибке должно быть в отрендеренном next-ответе.
        response_followed = self.client.post(url, {"email": "flood2@example.com"}, follow=True)
        self.assertContains(response_followed, "Слишком много попыток")


@override_settings(CACHES=LOCMEM_CACHES)
class VerifyEmailRateLimitTests(TestCase):
    """Различаем 'лимит сработал' от 'токен просто не найден' по
    ПОБОЧНОМУ ЭФФЕКТУ: если бы лимит НЕ сработал, валидный токен верифицировал
    бы пользователя. Если лимит сработал — запрос обязан развернуться ДО
    похода в БД за пользователем, и is_verified должен остаться False."""

    def setUp(self):
        cache.clear()
        from users.views import VERIFY_EMAIL_RATE_LIMIT
        self.limit = VERIFY_EMAIL_RATE_LIMIT
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123", is_verified=False)

    def test_exceeding_limit_blocks_before_user_lookup(self):
        import uuid

        # "Разогрев" бакета ЧУЖИМ/несуществующим токеном — ключ лимита в
        # VerifyEmailView построен по IP, а не по токену (см. docstring
        # класса), так что для исчерпания бакета не важно, какой токен
        # использовать. Реальный токен пользователя намеренно бережём
        # нетронутым до последнего запроса — иначе он верифицировался бы
        # уже на первом же "разогревочном" вызове и тест перестал бы что-
        # либо проверять (is_verified стал бы True ДО проверки лимита).
        bogus_url = reverse("users:verify_email", args=[uuid.uuid4()])
        for _ in range(self.limit):
            self.client.get(bogus_url)

        real_url = reverse("users:verify_email", args=[self.user.verification_token])
        self.client.get(real_url)  # (limit+1)-й запрос с тем же IP — должен быть заблокирован
        self.user.refresh_from_db()
        self.assertFalse(
            self.user.is_verified,
            "запрос сверх лимита не должен был дойти до верификации пользователя",
        )

    def test_within_limit_verifies_user_normally(self):
        url = reverse("users:verify_email", args=[self.user.verification_token])
        self.client.get(url)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_verified)


@override_settings(CACHES=LOCMEM_CACHES)
class ToggleFollowRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()
        from users.views import FOLLOW_RATE_LIMIT
        self.limit = FOLLOW_RATE_LIMIT
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pass123")
        self.player = _make_player()
        self.client.force_login(self.user)
        self.url = reverse("users:toggle_follow", args=["player", self.player.id])

    def test_exceeding_limit_returns_429_and_does_not_toggle(self):
        for _ in range(self.limit):
            self.client.post(self.url)

        from users.models import Follow
        state_before = Follow.objects.filter(user=self.user, player=self.player).exists()

        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 429)
        state_after = Follow.objects.filter(user=self.user, player=self.player).exists()
        self.assertEqual(state_before, state_after, "заблокированный запрос не должен менять состояние подписки")

    def test_within_limit_returns_200(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 200)
