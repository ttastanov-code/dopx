# api/tests.py
"""Тесты DRF API: права доступа (IsAuthenticatedAndVerified, VotingOpenPermission),
публичные агрегаты, отсутствие утечки личных полей, EvaluationPolicy.
CACHES -> LocMemCache.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from evaluations.models import ContextEvaluation, PlayerEvaluation
from leagues.models import League
from matches.models import Match
from players.models import Player
from seasons.models import Season
from teams.models import Team

User = get_user_model()

LOCMEM_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-api-cache",
    }
}


def _make_match(voting_open_until=None, start_time=None):
    """Фикстуры создаются заново на каждый вызов."""
    n = Match.objects.count()
    league = League.objects.create(name=f"League-{n}", country="KZ")
    season = Season.objects.create(league=league, year="2026")
    home = Team.objects.create(name=f"Home-{n}")
    away = Team.objects.create(name=f"Away-{n}")
    return Match.objects.create(
        league=league,
        season=season,
        home_team=home,
        away_team=away,
        start_time=start_time or (timezone.now() - timedelta(hours=2)),
        voting_open_until=voting_open_until or (timezone.now() + timedelta(hours=48)),
        status="finished",
    )


def _make_player(team):
    return Player.objects.create(first_name="Иван", last_name="Иванов", team=team, number=10)


def _make_verified_user(username="verified"):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pass12345", is_verified=True
    )


def _make_unverified_user(username="unverified"):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pass12345", is_verified=False
    )


@override_settings(CACHES=LOCMEM_CACHES)
class IsAuthenticatedAndVerifiedSweepTests(APITestCase):
    """Все 6 write-ViewSet'ов оценок: аноним -> 403, неверифицированный -> 403, верифицированный -> 200.
    403, а не 401 — первым стоит SessionAuthentication, у него нет WWW-Authenticate.
    """

    def setUp(self):
        cache.clear()

    WRITE_LIST_URL_NAMES = (
        "api:context-eval-list",
        "api:team-eval-list",
        "api:player-eval-list",
        "api:coach-eval-list",
        "api:referee-eval-list",
        "api:match-eval-list",
    )

    def test_anonymous_gets_403_on_every_write_endpoint(self):
        for url_name in self.WRITE_LIST_URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                # 403, не 401 — важно, что запрос отклонён.
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN,
                    f"{url_name}: анонимный доступ должен быть отклонён (403), а не {response.status_code}",
                )

    def test_unverified_user_gets_403_on_every_write_endpoint(self):
        user = _make_unverified_user()
        self.client.force_authenticate(user=user)
        for url_name in self.WRITE_LIST_URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(
                    response.status_code, status.HTTP_403_FORBIDDEN,
                    f"{url_name}: неверифицированный аккаунт должен получать 403 "
                    f"(это весь смысл IsAuthenticatedAndVerified), а не {response.status_code}",
                )

    def test_verified_user_gets_200_on_every_write_endpoint(self):
        user = _make_verified_user()
        self.client.force_authenticate(user=user)
        for url_name in self.WRITE_LIST_URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, status.HTTP_200_OK, url_name)


@override_settings(CACHES=LOCMEM_CACHES)
class ContextEvaluationAPITests(APITestCase):
    """ContextEvaluationViewSet: создание, дубликат, закрытое голосование, поля ответа."""

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.client.force_authenticate(user=self.user)
        self.match = _make_match()
        self.team = self.match.home_team

    def test_create_context_evaluation(self):
        url = reverse("api:context-eval-list")
        response = self.client.post(
            url, {"match": str(self.match.id), "supported_team": str(self.team.id), "watched_type": "full"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            ContextEvaluation.objects.filter(user=self.user, match=self.match).exists()
        )

    def test_duplicate_context_evaluation_rejected(self):
        """Повторный голос за матч — 400, а не IntegrityError."""
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        url = reverse("api:context-eval-list")
        response = self.client.post(url, {"match": str(self.match.id), "watched_type": "highlights"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_vote_after_voting_closed(self):
        closed_match = _make_match(voting_open_until=timezone.now() - timedelta(hours=1))
        url = reverse("api:context-eval-list")
        response = self.client.post(url, {"match": str(closed_match.id), "watched_type": "full"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_only_returns_own_evaluations_not_other_users(self):
        other = _make_verified_user(username="other")
        ContextEvaluation.objects.create(user=other, match=self.match, watched_type="full")
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="highlights")

        response = self.client.get(reverse("api:context-eval-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(results), 1)

    def test_response_does_not_leak_user_field(self):
        """В ответе нет user."""
        response = self.client.post(
            reverse("api:context-eval-list"), {"match": str(self.match.id), "watched_type": "full"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        for leaking_field in ("user", "user_id", "email", "password", "ip_address"):
            self.assertNotIn(leaking_field, response.data, f"поле {leaking_field!r} не должно быть в ответе API")


@override_settings(CACHES=LOCMEM_CACHES)
class PlayerEvaluationAPITests(APITestCase):
    """PlayerEvaluationViewSet: нужен ContextEvaluation, диапазон 1..10, by_match и analytics."""

    def setUp(self):
        from lineups.models import MatchLineup, MatchLineupPlayer

        cache.clear()
        self.user = _make_verified_user()
        self.client.force_authenticate(user=self.user)
        self.match = _make_match()
        self.player = _make_player(self.match.home_team)
        # Игрок должен быть в заявке матча.
        lineup = MatchLineup.objects.create(match=self.match, team=self.match.home_team, side="home")
        MatchLineupPlayer.objects.create(lineup=lineup, player=self.player, is_starting=True, shirt_number=10)

    def test_create_without_prior_context_evaluation_rejected(self):
        """Без ContextEvaluation — 400."""
        url = reverse("api:player-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "player": str(self.player.id), "contribution": 8, "risk": 3, "potential": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(PlayerEvaluation.objects.filter(user=self.user, player=self.player).exists())

    def test_create_after_context_evaluation_succeeds(self):
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        url = reverse("api:player-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "player": str(self.player.id), "contribution": 8, "risk": 3, "potential": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_contribution_out_of_range_rejected(self):
        """Диапазон 1..10 проверяется на уровне API."""
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        url = reverse("api:player-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "player": str(self.player.id), "contribution": 11, "risk": 3, "potential": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_by_match_action_requires_verified_account(self):
        """by_match закрыт так же, как весь ViewSet."""
        self.client.force_authenticate(user=None)
        url = reverse("api:player-eval-by-match")
        response = self.client.get(url, {"match_id": str(self.match.id)})
        # 403, не 401 — важно, что запрос отклонён.
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_by_match_action_returns_evaluations_for_match(self):
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        PlayerEvaluation.objects.create(
            user=self.user, match=self.match, player=self.player, contribution=8, risk=3, potential=7
        )
        url = reverse("api:player-eval-by-match")
        response = self.client.get(url, {"match_id": str(self.match.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertNotIn("user", response.data[0])

    def test_by_match_action_requires_match_id_param(self):
        url = reverse("api:player-eval-by-match")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_analytics_action_requires_player_id_param(self):
        url = reverse("api:player-eval-analytics")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_analytics_action_returns_summary_shape(self):
        url = reverse("api:player-eval-analytics")
        response = self.client.get(url, {"player_id": str(self.player.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("aggregates", response.data)
        self.assertIn("summary", response.data)
        self.assertIn("total_matches", response.data["summary"])


@override_settings(CACHES=LOCMEM_CACHES)
class VotingOpenPermissionObjectLevelTests(APITestCase):
    """После закрытия голосования владелец не может изменить оценку."""

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.client.force_authenticate(user=self.user)

    def test_owner_cannot_update_after_voting_closed(self):
        match = _make_match(voting_open_until=timezone.now() - timedelta(hours=1))
        evaluation = ContextEvaluation.objects.create(user=self.user, match=match, watched_type="full")

        url = reverse("api:context-eval-detail", args=[evaluation.id])
        response = self.client.patch(url, {"watched_type": "highlights"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        evaluation.refresh_from_db()
        self.assertEqual(evaluation.watched_type, "full", "оценка не должна была измениться")

    def test_owner_can_still_read_after_voting_closed(self):
        """Чтение разрешено всегда."""
        match = _make_match(voting_open_until=timezone.now() - timedelta(hours=1))
        evaluation = ContextEvaluation.objects.create(user=self.user, match=match, watched_type="full")

        url = reverse("api:context-eval-detail", args=[evaluation.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_owner_can_update_while_voting_open(self):
        match = _make_match()
        evaluation = ContextEvaluation.objects.create(user=self.user, match=match, watched_type="full")

        url = reverse("api:context-eval-detail", args=[evaluation.id])
        response = self.client.patch(url, {"watched_type": "highlights"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_cannot_access_another_users_evaluation_by_id(self):
        """Чужой id -> 404."""
        other = _make_verified_user(username="other-owner")
        match = _make_match()
        other_evaluation = ContextEvaluation.objects.create(user=other, match=match, watched_type="full")

        url = reverse("api:context-eval-detail", args=[other_evaluation.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(CACHES=LOCMEM_CACHES)
class AggregateViewSetsPublicAccessTests(APITestCase):
    """Агрегаты — намеренно публичные (AllowAny) для embed-виджетов."""

    def setUp(self):
        cache.clear()
        self.match = _make_match()
        self.player = _make_player(self.match.home_team)

    LIST_URL_NAMES = (
        "api:match-aggregate-list",
        "api:player-aggregate-list",
        "api:coach-aggregate-list",
    )

    def test_anonymous_access_returns_200_not_401_or_403(self):
        for url_name in self.LIST_URL_NAMES:
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, status.HTTP_200_OK, url_name)

    def test_recent_aggregates_action_is_public(self):
        url = reverse("api:match-aggregate-recent")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_top_players_action_is_public(self):
        url = reverse("api:player-aggregate-top-players")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_by_season_action_requires_season_id_param(self):
        url = reverse("api:player-aggregate-by-season")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_by_season_action_is_public(self):
        url = reverse("api:player-aggregate-by-season")
        response = self.client.get(url, {"season_id": str(self.match.season_id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_player_aggregate_public_fields_only(self):
        """Публичный агрегат не отдаёт ничего лишнего."""
        from aggregates.models import PlayerMatchAggregate

        PlayerMatchAggregate.objects.create(
            player=self.player, match=self.match, avg_contribution=7.5, total_votes=3, performance_score=6.2
        )
        response = self.client.get(reverse("api:player-aggregate-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(len(results), 1)
        payload = results[0]
        for leaking_field in ("user", "user_id", "email", "password", "ip_address", "registration_ip"):
            self.assertNotIn(leaking_field, payload)


@override_settings(CACHES=LOCMEM_CACHES)
class SerializerFieldLeakageTests(APITestCase):
    """Ни один сериалайзер оценок не отдаёт личные поля."""

    FORBIDDEN_KEYS = ("user", "user_id", "email", "password", "password_hash", "ip_address", "registration_ip")

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.match = _make_match()
        self.team = self.match.home_team
        self.player = _make_player(self.team)

    def _assert_no_forbidden_keys(self, data: dict):
        for key in self.FORBIDDEN_KEYS:
            self.assertNotIn(key, data)
        # Вложенный match_details тоже проверяем.
        if "match_details" in data and data["match_details"]:
            for key in self.FORBIDDEN_KEYS:
                self.assertNotIn(key, data["match_details"])

    def test_context_evaluation_serializer_fields(self):
        from api.serializers import ContextEvaluationSerializer

        obj = ContextEvaluation.objects.create(
            user=self.user, match=self.match, supported_team=self.team, watched_type="full"
        )
        self._assert_no_forbidden_keys(ContextEvaluationSerializer(obj).data)

    def test_player_evaluation_serializer_fields(self):
        from api.serializers import PlayerEvaluationSerializer

        obj = PlayerEvaluation.objects.create(
            user=self.user, match=self.match, player=self.player, contribution=8, risk=3, potential=7
        )
        self._assert_no_forbidden_keys(PlayerEvaluationSerializer(obj).data)

    def test_team_evaluation_serializer_fields(self):
        from api.serializers import TeamEvaluationSerializer
        from evaluations.models import TeamEvaluation

        obj = TeamEvaluation.objects.create(
            user=self.user, match=self.match, team=self.team, tactics=7, effort=8, organization=6, mentality=7
        )
        self._assert_no_forbidden_keys(TeamEvaluationSerializer(obj).data)

    def test_coach_evaluation_serializer_fields(self):
        from api.serializers import CoachEvaluationSerializer
        from evaluations.models import CoachEvaluation
        from coaches.models import Coach

        coach = Coach.objects.create(first_name="Тренер", last_name="Тренеров", team=self.team)
        obj = CoachEvaluation.objects.create(
            user=self.user, match=self.match, coach=coach, tactics=7, substitutions=6, game_management=8, impact=7
        )
        self._assert_no_forbidden_keys(CoachEvaluationSerializer(obj).data)

    def test_referee_evaluation_serializer_fields(self):
        from api.serializers import RefereeEvaluationSerializer
        from evaluations.models import RefereeEvaluation

        obj = RefereeEvaluation.objects.create(
            user=self.user, match=self.match, influence_score=40, decision_quality=7
        )
        self._assert_no_forbidden_keys(RefereeEvaluationSerializer(obj).data)

    def test_match_evaluation_serializer_fields(self):
        from api.serializers import MatchEvaluationSerializer
        from evaluations.models import MatchEvaluation

        obj = MatchEvaluation.objects.create(
            user=self.user, match=self.match, entertainment=8, tension=7, turning_point=True, fairness=6
        )
        self._assert_no_forbidden_keys(MatchEvaluationSerializer(obj).data)


@override_settings(CACHES=LOCMEM_CACHES)
class EvaluationPolicyAPITests(APITestCase):
    """EvaluationPolicy: сущность не из матча -> 400."""

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.client.force_authenticate(user=self.user)
        self.match = _make_match()
        # Чужой матч/команда/игрок/тренер для негативных кейсов.
        self.other_match = _make_match()
        self.other_team = Team.objects.create(name="Стороння команда")
        self.other_player = _make_player(self.other_team)

    def _add_context(self, match=None):
        ContextEvaluation.objects.create(
            user=self.user, match=match or self.match, watched_type="full"
        )

    def test_player_not_in_squad_rejected(self):
        """Игрок не из заявки матча — отклоняется."""
        self._add_context()
        url = reverse("api:player-eval-list")
        response = self.client.post(
            url,
            {
                "match": str(self.match.id),
                "player": str(self.other_player.id),
                "contribution": 8,
                "risk": 3,
                "potential": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertFalse(
            PlayerEvaluation.objects.filter(user=self.user, player=self.other_player).exists()
        )

    def test_player_in_squad_accepted(self):
        """Игрок из заявки — проходит."""
        from lineups.models import MatchLineup, MatchLineupPlayer

        lineup = MatchLineup.objects.create(match=self.match, team=self.match.home_team, side="home")
        player = _make_player(self.match.home_team)
        MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=True, shirt_number=9)
        self._add_context()

        url = reverse("api:player-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "player": str(player.id), "contribution": 8, "risk": 3, "potential": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_team_not_in_match_rejected(self):
        self._add_context()
        url = reverse("api:team-eval-list")
        response = self.client.post(
            url,
            {
                "match": str(self.match.id),
                "team": str(self.other_team.id),
                "tactics": 7,
                "effort": 8,
                "organization": 6,
                "mentality": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_team_in_match_accepted(self):
        self._add_context()
        url = reverse("api:team-eval-list")
        response = self.client.post(
            url,
            {
                "match": str(self.match.id),
                "team": str(self.match.home_team_id),
                "tactics": 7,
                "effort": 8,
                "organization": 6,
                "mentality": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_coach_not_in_match_rejected(self):
        from coaches.models import Coach

        self._add_context()
        outside_coach = Coach.objects.create(first_name="Чужой", last_name="Тренер", team=self.other_team)
        url = reverse("api:coach-eval-list")
        response = self.client.post(
            url,
            {
                "match": str(self.match.id),
                "coach": str(outside_coach.id),
                "tactics": 7,
                "substitutions": 6,
                "game_management": 8,
                "impact": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_coach_in_match_accepted(self):
        from coaches.models import Coach

        self._add_context()
        home_coach = Coach.objects.create(first_name="Свой", last_name="Тренер", team=self.match.home_team)
        self.match.home_coach = home_coach
        self.match.save(update_fields=["home_coach"])

        url = reverse("api:coach-eval-list")
        response = self.client.post(
            url,
            {
                "match": str(self.match.id),
                "coach": str(home_coach.id),
                "tactics": 7,
                "substitutions": 6,
                "game_management": 8,
                "impact": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_supported_team_not_in_match_rejected(self):
        """supported_team — только хозяева или гости этого матча."""
        url = reverse("api:context-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "supported_team": str(self.other_team.id), "watched_type": "full"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_coach_evaluation_without_context_rejected(self):
        """Контекст просмотра нужен для любого типа оценки."""
        from coaches.models import Coach

        home_coach = Coach.objects.create(first_name="Свой", last_name="Тренер", team=self.match.home_team)
        self.match.home_coach = home_coach
        self.match.save(update_fields=["home_coach"])
        url = reverse("api:coach-eval-list")
        response = self.client.post(
            url,
            {
                "match": str(self.match.id), "coach": str(home_coach.id),
                "tactics": 7, "substitutions": 6, "game_management": 8, "impact": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_referee_evaluation_without_context_rejected(self):
        url = reverse("api:referee-eval-list")
        response = self.client.post(
            url, {"match": str(self.match.id), "influence_score": 40, "decision_quality": 7}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

    def test_match_evaluation_without_context_rejected(self):
        url = reverse("api:match-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "entertainment": 8, "tension": 7, "fairness": 6, "turning_point": False},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)


@override_settings(CACHES=LOCMEM_CACHES)
class EvaluationIdentityImmutableOnUpdateAPITests(APITestCase):
    """PATCH не может подменить identity-поля (match/player/team/coach/supported_team)."""

    def setUp(self):
        from lineups.models import MatchLineup, MatchLineupPlayer

        cache.clear()
        self.user = _make_verified_user()
        self.client.force_authenticate(user=self.user)
        self.match = _make_match()
        self.other_match = _make_match()

        self.player = _make_player(self.match.home_team)
        self.other_player = _make_player(self.match.away_team)
        home_lineup = MatchLineup.objects.create(match=self.match, team=self.match.home_team, side="home")
        away_lineup = MatchLineup.objects.create(match=self.match, team=self.match.away_team, side="away")
        MatchLineupPlayer.objects.create(lineup=home_lineup, player=self.player, is_starting=True, shirt_number=10)
        MatchLineupPlayer.objects.create(lineup=away_lineup, player=self.other_player, is_starting=True, shirt_number=9)

        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        ContextEvaluation.objects.create(user=self.user, match=self.other_match, watched_type="full")

    def test_cannot_change_player_via_patch(self):
        """other_player тоже в заявке — проверка именно на запрет смены поля."""
        obj = PlayerEvaluation.objects.create(
            user=self.user, match=self.match, player=self.player, contribution=8, risk=3, potential=7
        )
        url = reverse("api:player-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"player": str(self.other_player.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.player_id, self.player.id)

    def test_cannot_change_match_via_patch(self):
        obj = PlayerEvaluation.objects.create(
            user=self.user, match=self.match, player=self.player, contribution=8, risk=3, potential=7
        )
        url = reverse("api:player-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"match": str(self.other_match.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.match_id, self.match.id)

    def test_can_still_patch_score_fields(self):
        """PATCH баллов работает."""
        obj = PlayerEvaluation.objects.create(
            user=self.user, match=self.match, player=self.player, contribution=8, risk=3, potential=7
        )
        url = reverse("api:player-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"contribution": 5})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.contribution, 5)

    def test_cannot_change_team_via_patch(self):
        from evaluations.models import TeamEvaluation

        obj = TeamEvaluation.objects.create(
            user=self.user, match=self.match, team=self.match.home_team,
            tactics=7, effort=8, organization=6, mentality=7,
        )
        url = reverse("api:team-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"team": str(self.match.away_team_id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.team_id, self.match.home_team_id)

    def test_cannot_change_coach_via_patch(self):
        from coaches.models import Coach
        from evaluations.models import CoachEvaluation

        home_coach = Coach.objects.create(first_name="Домашний", last_name="Тренер", team=self.match.home_team)
        away_coach = Coach.objects.create(first_name="Гостевой", last_name="Тренер", team=self.match.away_team)
        self.match.home_coach = home_coach
        self.match.away_coach = away_coach
        self.match.save(update_fields=["home_coach", "away_coach"])

        obj = CoachEvaluation.objects.create(
            user=self.user, match=self.match, coach=home_coach,
            tactics=7, substitutions=6, game_management=8, impact=7,
        )
        url = reverse("api:coach-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"coach": str(away_coach.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.coach_id, home_coach.id)

    def test_cannot_change_match_via_patch_on_referee_evaluation(self):
        from evaluations.models import RefereeEvaluation

        obj = RefereeEvaluation.objects.create(
            user=self.user, match=self.match, influence_score=40, decision_quality=7
        )
        url = reverse("api:referee-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"match": str(self.other_match.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.match_id, self.match.id)

    def test_cannot_change_match_via_patch_on_match_evaluation(self):
        from evaluations.models import MatchEvaluation

        obj = MatchEvaluation.objects.create(
            user=self.user, match=self.match, entertainment=8, tension=7, turning_point=False, fairness=6
        )
        url = reverse("api:match-eval-detail", args=[obj.id])
        response = self.client.patch(url, {"match": str(self.other_match.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertEqual(obj.match_id, self.match.id)

    def test_cannot_change_match_or_supported_team_via_patch_on_context_evaluation(self):
        obj = ContextEvaluation.objects.filter(user=self.user, match=self.match).get()
        url = reverse("api:context-eval-detail", args=[obj.id])

        response = self.client.patch(url, {"match": str(self.other_match.id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)

        response = self.client.patch(url, {"supported_team": str(self.match.away_team_id)})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        obj.refresh_from_db()
        self.assertIsNone(obj.supported_team_id)


@override_settings(CACHES=LOCMEM_CACHES)
class MatchEvaluationSummaryActionTests(APITestCase):
    """MatchEvaluationViewSet.summary закрыт так же, как весь ViewSet."""

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.match = _make_match()

    def test_summary_requires_authentication(self):
        url = reverse("api:match-eval-summary")
        response = self.client.get(url, {"match_id": str(self.match.id)})
        # 403, не 401 — важно, что запрос отклонён.
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_summary_requires_match_id_param(self):
        self.client.force_authenticate(user=self.user)
        url = reverse("api:match-eval-summary")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_summary_returns_expected_shape(self):
        self.client.force_authenticate(user=self.user)
        url = reverse("api:match-eval-summary")
        response = self.client.get(url, {"match_id": str(self.match.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("match", response.data)
        self.assertIn("stats", response.data)
        self.assertIn("total_match_evaluations", response.data["stats"])
