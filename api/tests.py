# api/tests.py
"""
Тесты публичного DRF API (api/), который читают embed-виджеты на сторонних
сайтах и через который проходит запись голосов вайзарда оценки матча.

До этой сессии здесь не было ни одного теста, при этом это единственное
приложение, чьи write-эндпоинты защищены НЕ голым `IsAuthenticated`, а
кастомным `IsAuthenticatedAndVerified` (api/permissions.py) — специально,
чтобы неверифицированный (не подтвердивший email) аккаунт не мог голосовать
через прямой вызов API в обход html-вайзарда (см. докстринг api/views.py).
Раз весь смысл этого класса — в дополнительной проверке `is_verified`, а не
только `is_authenticated`, ключевой тест здесь — именно связка "authenticated,
но НЕ verified" должна блокироваться так же, как анонимный запрос.

Разделение по классам:
  * `IsAuthenticatedAndVerifiedSweepTests` — прогоняет анонимный /
    неверифицированный / верифицированный доступ по ВСЕМ 6 write-ViewSet'ам
    (Context/Team/Player/Coach/Referee/MatchEval) одним циклом, а не 6
    почти идентичными классами: permission_classes у них буквально одна и
    та же строка (`[IsAuthenticatedAndVerified, VotingOpenPermission]`),
    дублировать проверку 6 раз — не добавлять сигнала, а увеличивать
    поверхность, которую придётся чинить при следующем рефакторинге.
  * `ContextEvaluationAPITests` / `PlayerEvaluationAPITests` — один
    представитель write-эндпоинта разобран подробно (create, дубликат,
    голосование закрыто, поля сериалайзера), плюс кастомные @action
    (`by_match`, `analytics`) у PlayerEvaluationViewSet.
  * `VotingOpenPermissionObjectLevelTests` — отдельная проверка
    object-level части permission-стека: `VotingOpenPermission` блокирует
    ИЗМЕНЕНИЕ уже существующей оценки после закрытия голосования, даже
    владельцу (has_object_permission, а не has_permission — другой путь
    в DRF, стоит отдельного теста).
  * `AggregateViewSetsPublicAccessTests` — три read-only ViewSet'а
    (MatchAggregate/PlayerAggregate/CoachAggregate) — это ОСОЗНАННО
    публичные (`permissions.AllowAny`) эндпоинты для embed-виджетов на
    сторонних сайтах, тесты фиксируют это как ожидаемое поведение и
    проверяют, что наружу течёт только предназначенный для паблика набор
    полей (никаких `user`, email, IP и т.п. — их в сериалайзерах и так нет,
    но именно ЭТО и должно остаться неизменным инвариантом).
  * `SerializerFieldLeakageTests` — по каждому сериалайзеру оценок отдельно
    проверяет отсутствие чувствительных полей в `.data` на реальном
    экземпляре, а не только "не упомянуто в Meta.fields" (человек, читающий
    код, мог упустить, что поле утекает окольным путём через SerializerMethodField
    или related-объект).

CACHES переопределены на LocMemCache (как в core/tests.py): прод использует
Redis (dopx/settings.py::CACHES), но почти каждый write/read метод во
`api/views.py` явно трогает `django.core.cache.cache` (инвалидация агрегатов,
кэширование ответов `by_match`/`analytics`/`recent`/`top_players`) и throttle-
классы DRF тоже читают/пишут через cache-backend — тест не должен зависеть от
того, поднят ли Redis на машине, где запускается `manage.py test`.
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
    """Лига/сезон/команды создаются по одной на вызов (никаких get_or_create по
    имени) — совпадение имён между тестами не должно склеивать fixtures разных
    тестовых методов через общий UniqueConstraint."""
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
    """
    Один и тот же permission-стек (`[IsAuthenticatedAndVerified,
    VotingOpenPermission]`) висит буквально на всех 6 write-ViewSet'ов оценок
    — проверяем это одним циклом по `list`-эндпоинту каждого (не требует
    создания объектов, GET тоже блокируется, т.к. IsAuthenticatedAndVerified
    проверяется в `has_permission`, до диспетчеризации по методу).

    Ожидаемые статусы:
      * анонимный запрос -> 403, НЕ 401. Изначально здесь ожидался 401 —
        рассуждение было "BasicAuthentication зарегистрирован → он отдаёт
        WWW-Authenticate → NotAuthenticated (401)". Реальный прогон против
        Postgres (2026-08-28) это опроверг: DRF выбирает заголовок
        WWW-Authenticate не у ЛЮБОГО настроенного authenticator'а, а строго у
        ПЕРВОГО в списке (`APIView.get_authenticate_header()` →
        `self.get_authenticators()[0].authenticate_header(request)`). В
        dopx/settings.py::REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']
        первым стоит `SessionAuthentication`, а у него `authenticate_header()`
        не переопределён (наследует `BaseAuthentication`, который возвращает
        `None` — у сессионной аутентификации нет протокола "предъявить
        challenge браузеру"). Раз заголовка нет — `APIView.handle_exception()`
        сам понижает `NotAuthenticated` до `PermissionDenied` (403), даже
        притом что `BasicAuthentication` вторым в списке МОГ БЫ отдать
        `WWW-Authenticate`, если бы его спросили. На реальный контроль
        доступа это не влияет: анонимный запрос в обоих случаях отклоняется,
        разница чисто в HTTP-статусе. Если когда-нибудь понадобится
        настоящий 401 (например, для стороннего клиента, ожидающего RFC
        7235-corrent поведение) — исправление в one line: поменять местами
        `SessionAuthentication`/`BasicAuthentication` в settings.py.
      * authenticated, но is_verified=False -> 403 (`successful_authenticator`
        уже есть, дальше именно `PermissionDenied`, тут разногласий не было).
      * authenticated и is_verified=True -> 200.
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
                # 403, не 401 — см. докстринг класса выше про порядок
                # DEFAULT_AUTHENTICATION_CLASSES. Важно то, что запрос
                # ОТКЛОНЁН, а не конкретный код.
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
    """ContextEvaluationViewSet — первый шаг вайзарда, разобран подробно как
    представитель write-эндпоинта: создание, дубликат, закрытое голосование,
    и что конкретно отдаёт сериалайзер."""

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
        """Уникальность user+match обеспечена и на уровне БД (UniqueConstraint),
        и на уровне сериалайзера (validate()) — второй голос за тот же матч
        должен быть отклонён валидацией (400), а не 500 от IntegrityError."""
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
        """ContextEvaluationSerializer.Meta.fields не перечисляет `user` —
        подтверждаем это на реальном ответе, не только чтением исходника:
        чужой email/username/id не должен утекать через API оценок."""
        response = self.client.post(
            reverse("api:context-eval-list"), {"match": str(self.match.id), "watched_type": "full"}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        for leaking_field in ("user", "user_id", "email", "password", "ip_address"):
            self.assertNotIn(leaking_field, response.data, f"поле {leaking_field!r} не должно быть в ответе API")


@override_settings(CACHES=LOCMEM_CACHES)
class PlayerEvaluationAPITests(APITestCase):
    """PlayerEvaluationViewSet — самый насыщенный write-ViewSet: требует
    предварительного ContextEvaluation, диапазон 1..10 на все три поля,
    плюс два кастомных read-@action (by_match, analytics)."""

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.client.force_authenticate(user=self.user)
        self.match = _make_match()
        self.player = _make_player(self.match.home_team)

    def test_create_without_prior_context_evaluation_rejected(self):
        """PlayerEvaluationSerializer.validate() требует, чтобы ContextEvaluation
        для этого матча уже существовал ("Сначала укажите контекст просмотра
        матча") — прямой вызов API в обход шага 1 вайзарда должен падать."""
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
        """Модель ограничивает 1..10 валидаторами, но проверяем это через API,
        а не только на уровне модели — именно сюда прилетает необработанный
        пользовательский ввод."""
        ContextEvaluation.objects.create(user=self.user, match=self.match, watched_type="full")
        url = reverse("api:player-eval-list")
        response = self.client.post(
            url,
            {"match": str(self.match.id), "player": str(self.player.id), "contribution": 11, "risk": 3, "potential": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_by_match_action_requires_verified_account(self):
        """`by_match` — read-эндпоинт, но наследует permission_classes всего
        ViewSet'а (никакого override на @action) — значит тоже закрыт
        IsAuthenticatedAndVerified, а не публичный, в отличие от аггрегатов."""
        self.client.force_authenticate(user=None)
        url = reverse("api:player-eval-by-match")
        response = self.client.get(url, {"match_id": str(self.match.id)})
        # 403, не 401 — см. докстринг IsAuthenticatedAndVerifiedSweepTests
        # (порядок DEFAULT_AUTHENTICATION_CLASSES понижает NotAuthenticated
        # до PermissionDenied). Важно, что запрос отклонён.
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
    """`VotingOpenPermission.has_object_permission` — отдельный путь в DRF от
    `has_permission` (проверяется в `get_object()`, только для retrieve/
    update/destroy УЖЕ существующего объекта, не для list/create). Владелец
    не должен иметь возможность отредактировать свою же оценку ПОСЛЕ того,
    как окно голосования для матча закрылось — иначе 48-часовой лимит
    голосования ничего не защищает."""

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
        """SAFE_METHODS всегда разрешены в VotingOpenPermission — просмотр
        уже поставленной оценки не должен пропадать после закрытия окна."""
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
        """get_queryset() фильтрует по `user=self.request.user` — чужой id в
        detail-URL должен давать 404, а не 200 с чужими данными или 403."""
        other = _make_verified_user(username="other-owner")
        match = _make_match()
        other_evaluation = ContextEvaluation.objects.create(user=other, match=match, watched_type="full")

        url = reverse("api:context-eval-detail", args=[other_evaluation.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


@override_settings(CACHES=LOCMEM_CACHES)
class AggregateViewSetsPublicAccessTests(APITestCase):
    """MatchAggregate/PlayerAggregate/CoachAggregate ViewSet'ы — ОСОЗНАННО
    `permissions.AllowAny` (см. api/views.py): это готовые агрегаты без
    персональных данных, предназначенные для встраиваемых виджетов на
    сторонних сайтах, у которых нет сессии/логина DOPX. Тесты фиксируют это
    поведение как намеренное, а не как забытый permission_classes."""

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
        """Публичный embed-эндпоинт не должен отдавать ничего, кроме того,
        что явно перечислено в PlayerMatchAggregateSerializer — никакого
        `user`/email/IP какого-либо голосовавшего (агрегат в принципе не
        привязан к конкретному голосовавшему, но фиксируем инвариант explicitly,
        чтобы будущий SerializerMethodField не добавил его случайно)."""
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
    """
    Точечная проверка на уровне сериалайзера (не только "не в Meta.fields",
    а прямо в `.data`) — по каждому из 6 сериалайзеров оценок. Виджеты,
    встраиваемые на СТОРОННИХ сайтах, читают этот же JSON — случайно
    добавленное `user`/email в любом из них означало бы утечку личных
    данных пользователей DOPX на чужие сайты.
    """

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
        # match_details — вложенный MatchSerializer, тоже проверяем.
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
class MatchEvaluationSummaryActionTests(APITestCase):
    """`MatchEvaluationViewSet.summary` — сводка по матчу (используется на
    странице результатов вайзарда). Как и `by_match`/`analytics` у
    PlayerEvaluationViewSet, это read-действие, но наследует
    permission_classes всего ViewSet'а — значит тоже требует верифицированный
    аккаунт, а не публичный агрегат."""

    def setUp(self):
        cache.clear()
        self.user = _make_verified_user()
        self.match = _make_match()

    def test_summary_requires_authentication(self):
        url = reverse("api:match-eval-summary")
        response = self.client.get(url, {"match_id": str(self.match.id)})
        # 403, не 401 — см. докстринг IsAuthenticatedAndVerifiedSweepTests
        # (порядок DEFAULT_AUTHENTICATION_CLASSES понижает NotAuthenticated
        # до PermissionDenied). Важно, что запрос отклонён.
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
