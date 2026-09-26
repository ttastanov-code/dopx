# core/tests_redesign.py
"""Панель «Ваш день», нижняя панель вкладок, липкая кнопка оценки, «Требует внимания», продолжение оценки."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from core.personal import personal_summary
from dashboard.models import StaffAccessGrant
from dashboard.services import attention_items
from evaluations.models import EvaluationSession
from leagues.models import League
from matches.models import Match
from seasons.models import Season
from teams.models import Team
from users.models import SuspiciousActivityFlag

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        league = League.objects.create(name="L", country="KZ", is_primary=True)
        self.season = Season.objects.create(league=league, year="2026", is_active=True)
        self.league = league
        self.home = Team.objects.create(name="Хозяева")
        self.away = Team.objects.create(name="Гости")
        self.user = User.objects.create_user(username="fan", email="fan@example.com", password="x", is_verified=True)

    def match(self, **extra):
        defaults = dict(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status="finished", start_time=timezone.now() - timedelta(hours=3),
            voting_open_until=timezone.now() + timedelta(hours=20),
        )
        defaults.update(extra)
        return Match.objects.create(**defaults)


class PersonalPanelTests(_Base):
    def test_summary_counts_only_unrated_open_matches(self):
        rated, open_one = self.match(), self.match()
        self.match(voting_open_until=timezone.now() - timedelta(hours=1))  # закрыт
        EvaluationSession.objects.create(user=self.user, match=rated, status="completed", completed_at=timezone.now())
        summary = personal_summary(self.user)
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["next_match"], open_one)

    def test_home_shows_panel_for_user_and_promo_for_guest(self):
        self.match()
        self.assertContains(self.client.get(reverse("core:home")), "Голос трибун")
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:home"))
        self.assertContains(response, "Привет, fan")
        self.assertContains(response, "1 матч ждёт вашей оценки")

    def test_resume_url_points_to_first_unfinished_step(self):
        match = self.match()
        session = EvaluationSession.objects.create(user=self.user, match=match, completed_steps=["context"])
        self.assertEqual(session.next_step_url(match.id), reverse("evaluations:teams", args=[match.id]))
        session.completed_steps = ["context", "teams", "players", "coaches", "referee"]
        self.assertEqual(session.next_step_url(match.id), reverse("evaluations:match_eval", args=[match.id]))


class TabbarAndStickyCtaTests(_Base):
    def test_tabbar_on_site_with_pending_badge_not_in_wizard(self):
        match = self.match()
        self.client.force_login(self.user)
        html = self.client.get(reverse("core:home")).content.decode()
        self.assertIn("dx-tabbar", html)
        self.assertIn('class="dx-tabbar__badge">1<', html)
        wizard = self.client.get(reverse("evaluations:context", args=[match.id])).content.decode()
        self.assertNotIn('class="dx-tabbar ', wizard)

    def test_sticky_cta_only_while_voting_and_not_rated(self):
        match = self.match()
        self.client.force_login(self.user)
        url = reverse("matches:detail", args=[match.id])
        self.assertContains(self.client.get(url), "dx-sticky-cta")
        EvaluationSession.objects.create(user=self.user, match=match, status="completed", completed_at=timezone.now())
        self.assertNotContains(self.client.get(url), 'class="dx-sticky-cta')


class AttentionTests(_Base):
    def test_items_respect_section_access_and_hide_zero(self):
        SuspiciousActivityFlag.objects.create(source="manual", status="pending")
        staff = User.objects.create_user(username="s", email="s@example.com", password="x", is_staff=True)
        StaffAccessGrant.objects.create(user=staff, allowed_sections=["overview", "names_review"])
        self.assertEqual(attention_items(staff), [])
        boss = User.objects.create_superuser(username="boss", email="b@example.com", password="x")
        items = attention_items(boss)
        self.assertEqual([(i["title"], i["count"]) for i in items], [("Сигналы антифрода", 1)])


class ReactionWidgetTests(_Base):
    def test_reaction_selected_with_fill(self):
        match = self.match(voting_open_until=timezone.now() - timedelta(days=1), home_score=1, away_score=0)
        self.client.force_login(self.user)
        response = self.client.post(reverse("matches:react", args=[match.id]), {"reaction": "upset"})
        html = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn("dx-react__btn--warning is-selected", html)
        self.assertIn("--fill: 100%", html)
        self.assertIn("<b>100%</b>", html)


class MatchListTabsTests(_Base):
    def test_status_tabs_and_bad_season_param(self):
        self.match()
        response = self.client.get(reverse("matches:list"), {"season": "junk"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "dx-chip is-active")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("matches:list")), "Оценить")


class FormChartTests(_Base):
    def test_player_form_points_chronological_with_opponent_from_lineup(self):
        from aggregates.models import PlayerMatchAggregate
        from lineups.models import MatchLineup, MatchLineupPlayer
        from players.models import Player

        player = Player.objects.create(first_name="Иван", last_name="Форма", team=self.home)
        closed = timezone.now() - timedelta(days=1)
        old = self.match(start_time=timezone.now() - timedelta(days=10), voting_open_until=closed)
        new = self.match(start_time=timezone.now() - timedelta(days=3), voting_open_until=closed)
        for m, score, votes in ((old, 6.0, 8), (new, 8.0, 2)):
            lineup = MatchLineup.objects.create(match=m, team=self.away, side="away")
            MatchLineupPlayer.objects.create(lineup=lineup, player=player, is_starting=True)
            PlayerMatchAggregate.objects.create(player=player, match=m, performance_score=score, total_votes=votes)
        response = self.client.get(reverse("players:detail", args=[player.id]))
        points = response.context["form_points"]
        self.assertEqual([p["url"] for p in points], [reverse("matches:detail", args=[old.id]), reverse("matches:detail", args=[new.id])])
        # В этих матчах играл за «Гостей» — соперник «Хозяева».
        self.assertEqual(points[0]["opponent_full"], "Хозяева")
        self.assertEqual(points[0]["score"], 6.0)
        self.assertIsNone(points[1]["score"])  # мало голосов
        self.assertContains(response, "Форма по матчам")


class NominationGroupsTests(TestCase):
    def test_grouped_by_category_best_first(self):
        from core.nominations import group_nominations
        noms = [
            {"entity_kind": "team", "sentiment": "negative", "key": "passive"},
            {"entity_kind": "referee", "sentiment": "negative", "key": "influential_referee"},
            {"entity_kind": "team", "sentiment": "positive", "key": "fighting"},
            {"entity_kind": "referee", "sentiment": "positive", "key": "fair"},
        ]
        groups = group_nominations(noms)
        self.assertEqual([g["title"] for g in groups], ["Судьи", "Команды"])
        self.assertEqual([n["key"] for n in groups[1]["items"]], ["fighting", "passive"])



class NominationsQueryTests(TestCase):
    def test_empty_db_returns_no_nominations(self):
        from core.nominations import get_nominations
        cache.clear()
        self.assertEqual(get_nominations(), [])

    def test_groups_and_highlights(self):
        from core.nominations import group_nominations, nomination_highlights
        noms = [{"entity_kind": "player", "sentiment": "positive", "key": k} for k in ("rising_talent", "player_of_season")]
        noms += [{"entity_kind": "player", "sentiment": "negative", "key": "player_disappointment"},
                 {"entity_kind": "team", "sentiment": "positive", "key": "team_of_season"}]
        groups = group_nominations(noms)
        self.assertEqual([n["key"] for n in groups[1]["items"]], ["player_of_season", "rising_talent", "player_disappointment"])
        self.assertEqual([n["key"] for n in nomination_highlights(noms)], ["player_of_season", "team_of_season"])


class PlatformStatsTests(_Base):
    def test_counts_completed_evaluations_and_real_users(self):
        from core.stats import platform_stats
        match = self.match()
        EvaluationSession.objects.create(user=self.user, match=match, status="completed", completed_at=timezone.now())
        EvaluationSession.objects.create(user=User.objects.create_user(username="q", email="q@example.com", password="x"), match=match)
        User.objects.create_user(username="off", email="off@example.com", password="x", is_active=False)
        cache.clear()
        stats = platform_stats()
        self.assertEqual(stats["total_evaluations"], 1)
        self.assertEqual(stats["active_users"], 1)
        self.assertEqual(stats["total_users"], 2)
        self.assertEqual(stats["total_matches"], 1)


class DataHealthStaleTests(_Base):
    def test_stale_live_and_scheduled_counted(self):
        from dashboard.services import data_health_summary
        self.match(status="live", start_time=timezone.now() - timedelta(hours=5))
        self.match(status="scheduled", start_time=timezone.now() - timedelta(hours=4))
        self.match(status="live", start_time=timezone.now() - timedelta(minutes=30))
        self.assertEqual(data_health_summary()["stale_matches"], 2)


class LeaderboardCountsCompletedOnlyTests(_Base):
    def test_unfinished_evaluation_not_in_leaderboard(self):
        match = self.match()
        EvaluationSession.objects.create(user=self.user, match=match, status="in_progress")
        self.assertEqual(list(self.client.get(reverse("users:leaderboard")).context["users"]), [])
        EvaluationSession.objects.filter(user=self.user).update(status="completed", completed_at=timezone.now())
        users = list(self.client.get(reverse("users:leaderboard")).context["users"])
        self.assertEqual([(u.username, u.eval_count) for u in users], [("fan", 1)])

    def test_next_other_skips_started_match(self):
        started, other = self.match(), self.match()
        EvaluationSession.objects.create(user=self.user, match=started, status="in_progress")
        self.assertEqual(personal_summary(self.user)["next_other"], other)


class LiveRefreshTests(_Base):
    def test_far_match_gets_wake_at_near_match_polls(self):
        far = self.match(status="scheduled", start_time=timezone.now() + timedelta(days=2))
        near = self.match(status="scheduled", start_time=timezone.now() + timedelta(minutes=30))
        self.assertIsNotNone(far.poll_wake_at)
        self.assertIsNone(near.poll_wake_at)
        self.assertIsNotNone(near.live_poll_seconds)
        html = self.client.get(reverse("matches:card", args=[far.id])).content.decode()
        self.assertIn("data-wake-at=", html)

    def test_personal_panel_partial(self):
        self.assertEqual(self.client.get(reverse("core:personal_panel")).status_code, 204)
        self.client.force_login(self.user)
        response = self.client.get(reverse("core:personal_panel"))
        self.assertContains(response, 'id="personal-panel"')
        self.assertContains(response, "Привет, fan")


class LiveRefreshSetupTests(_Base):
    def test_interval_attribute_and_wizard_off(self):
        self.assertContains(self.client.get(reverse("core:home")), 'data-live-refresh="30"')
        match = self.match()
        self.client.force_login(self.user)
        self.assertContains(self.client.get(reverse("evaluations:context", args=[match.id])), 'data-live-refresh="off"')

    def test_background_refresh_not_tracked(self):
        from unittest.mock import patch
        from django.test import RequestFactory
        from analytics.services import track_event
        request = RequestFactory().get("/", HTTP_X_LIVE_REFRESH="1")
        with patch("analytics.tasks.persist_event_task.delay") as delay:
            track_event("page_view", request=request)
        delay.assert_not_called()


class HumanizeScheduleTests(TestCase):
    def test_common_crontabs_and_intervals(self):
        from celery.schedules import crontab
        from dashboard.infra_services import BEAT_TASK_TITLES, beat_schedule_overview, humanize_schedule
        cases = {
            "каждые 10 минут": crontab(minute="*/10"),
            "каждый час в :05 и :35": crontab(minute="5,35"),
            "каждый день в 04:00": crontab(minute=0, hour=4),
            "по понедельникам в 10:00": crontab(minute=0, hour=10, day_of_week=1),
            "1-го числа каждого месяца в 03:00": crontab(minute=0, hour=3, day_of_month=1),
            "каждые 2 часа, в :15": crontab(minute=15, hour="*/2"),
            "каждые 15 секунд": 15.0,
        }
        for expected, schedule in cases.items():
            self.assertEqual(humanize_schedule(schedule), expected)
        entries = beat_schedule_overview()
        live = next(e for e in entries if e["name"] == "sportmonks-update-live")
        self.assertIsNone(live["next_run"])  # интервал: прошлый запуск неизвестен — без выдуманного отсчёта
        self.assertTrue(all(e["next_run"] for e in entries if not e["is_interval"]))
        self.assertTrue(all(e["description"] for e in entries))
