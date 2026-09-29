# engagement/tests.py
"""Тесты вовлечения: серия дней, сезонный пропуск, задания, приглашения, лиги, фан-зона, вьюхи."""
from __future__ import annotations

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from engagement import friend_leagues, quests, referrals, season, streaks
from engagement.models import DailyQuest, DailyStreak, FriendLeague, Referral, SeasonPass
from leagues.models import League
from matches.models import Match
from predictions.models import MatchPrediction
from seasons.models import Season
from teams.models import Team
from users.models import Follow, User, UserBadge, UserXP

LOCMEM_CACHE = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(CACHES=LOCMEM_CACHE)
class EngagementTestCase(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        push = mock.patch('notifications.tasks.send_push_task.delay')
        push.start()
        self.addCleanup(push.stop)
        self.league = League.objects.create(name="КПЛ", country="KZ")
        self.season = Season.objects.create(league=self.league, year="2026", is_active=True)
        self.home = Team.objects.create(name="Кайрат")
        self.away = Team.objects.create(name="Астана")
        self._n = 0

    def make_user(self, name="u", **extra):
        self._n += 1
        return User.objects.create_user(
            username=f"{name}{self._n}", email=f"{name}{self._n}@test.local", password="testpass123", **extra,
        )

    def make_match(self, status="finished", home_score=2, away_score=1, start=None, end=None):
        start = start or timezone.now() - timedelta(hours=3)
        return Match.objects.create(
            league=self.league, season=self.season, home_team=self.home, away_team=self.away,
            status=status, start_time=start, end_time=end if end is not None else start + timedelta(hours=2),
            voting_open_until=start + timedelta(days=2), home_score=home_score, away_score=away_score,
        )


class DailyStreakTests(EngagementTestCase):
    def test_consecutive_days_grow(self):
        s = DailyStreak(current=3, best=3, last_active_date=date(2026, 9, 1))
        self.assertTrue(streaks.apply_day(s, date(2026, 9, 2)))
        self.assertEqual((s.current, s.best), (4, 4))
        self.assertFalse(streaks.apply_day(s, date(2026, 9, 2)))

    def test_freeze_covers_missed_day(self):
        s = DailyStreak(current=10, best=10, freezes=1, last_active_date=date(2026, 9, 1))
        streaks.apply_day(s, date(2026, 9, 3))
        self.assertEqual((s.current, s.freezes, s.freezes_used), (11, 0, 1))

    def test_gap_without_freeze_resets(self):
        s = DailyStreak(current=10, best=10, freezes=0, last_active_date=date(2026, 9, 1))
        streaks.apply_day(s, date(2026, 9, 3))
        self.assertEqual((s.current, s.best), (1, 10))

    def test_freeze_every_seven_days_capped(self):
        s = DailyStreak(current=6, freezes=streaks.MAX_FREEZES - 1, last_active_date=date(2026, 9, 1))
        streaks.apply_day(s, date(2026, 9, 2))
        self.assertEqual(s.freezes, streaks.MAX_FREEZES)
        s.current, s.last_active_date = 13, date(2026, 9, 8)
        streaks.apply_day(s, date(2026, 9, 9))
        self.assertEqual(s.freezes, streaks.MAX_FREEZES)

    def test_touch_rewards_milestone_once(self):
        user = self.make_user()
        DailyStreak.objects.create(user=user, current=6, best=6, last_active_date=timezone.localdate() - timedelta(days=1))
        streaks.touch(user)
        streaks.touch(user)
        self.assertEqual(DailyStreak.objects.get(user=user).current, 7)
        self.assertTrue(UserBadge.objects.filter(user=user, badge_type="day_streak_7").exists())
        self.assertEqual(UserXP.objects.get(user=user).total_xp, streaks.MILESTONE_XP[7])

    def test_state_expired_streak_is_zero(self):
        user = self.make_user()
        DailyStreak.objects.create(user=user, current=5, best=5, last_active_date=timezone.localdate() - timedelta(days=5))
        self.assertEqual(streaks.state(user)["current"], 0)

    def test_middleware_counts_page_visit(self):
        user = self.make_user()
        self.client.force_login(user)
        self.client.get(reverse('engagement:season_pass'))
        streak = DailyStreak.objects.get(user=user)
        self.assertEqual((streak.current, streak.last_active_date), (1, timezone.localdate()))


class SeasonPassTests(EngagementTestCase):
    def test_level_for(self):
        self.assertEqual(season.level_for(0), 1)
        self.assertEqual(season.level_for(season.XP_PER_LEVEL), 2)
        self.assertEqual(season.level_for(10 ** 6), season.LEVELS)

    def test_rewards_and_cosmetics(self):
        user = self.make_user()
        season.add_season_xp(user.pk, season.XP_PER_LEVEL * 9)  # уровень 10
        sp = SeasonPass.objects.get(user=user, season=self.season)
        self.assertEqual(sorted(sp.claimed_levels), [5, 10])
        self.assertTrue(UserBadge.objects.filter(user=user, badge_type="season_pass_10").exists())
        self.assertEqual(season.cosmetics(user.pk), {"frame": "bronze", "golden_name": False})
        season.add_season_xp(user.pk, season.XP_PER_LEVEL * 30)
        self.assertEqual(season.cosmetics(user.pk), {"frame": "gold", "golden_name": True})

    def test_user_xp_feeds_season_pass(self):
        user = self.make_user()
        xp, _ = UserXP.objects.get_or_create(user=user)
        with self.captureOnCommitCallbacks(execute=True):
            xp.add_xp(42)
        self.assertEqual(SeasonPass.objects.get(user=user).xp, 42)

    def test_no_active_season_is_noop(self):
        Season.objects.update(is_active=False)
        user = self.make_user()
        season.add_season_xp(user.pk, 500)
        self.assertFalse(SeasonPass.objects.exists())
        self.assertIsNone(season.overview(user))


class DailyQuestTests(EngagementTestCase):
    def test_track_completes_and_all_done_bonus(self):
        user = self.make_user()
        today = timezone.localdate()
        DailyQuest.objects.create(user=user, date=today, key="predict", target=2, xp_reward=15)
        DailyQuest.objects.create(user=user, date=today, key="round", target=1, xp_reward=5)
        quests.track(user, "predict")
        self.assertFalse(DailyQuest.objects.get(user=user, key="predict").is_done)
        quests.track(user, "predict")
        quests.track(user, "round")
        quests.track(user, "round")  # повтор не даёт XP
        self.assertTrue(DailyQuest.objects.filter(user=user, key=quests.ALL_DONE_KEY).exists())
        self.assertEqual(UserXP.objects.get(user=user).total_xp, 15 + 5 + quests.ALL_DONE_XP)

    def test_today_quests_generated_once(self):
        user = self.make_user()
        first = quests.today_quests(user)
        second = quests.today_quests(user)
        self.assertLessEqual(len(first["items"]), quests.QUESTS_PER_DAY)
        self.assertEqual([q["key"] for q in first["items"]], [q["key"] for q in second["items"]])

    def test_prediction_signal_tracks_quest(self):
        user = self.make_user()
        DailyQuest.objects.create(user=user, date=timezone.localdate(), key="predict", target=1, xp_reward=15)
        match = self.make_match(status="scheduled", home_score=None, away_score=None,
                                start=timezone.now() + timedelta(days=1))
        with self.captureOnCommitCallbacks(execute=True):
            MatchPrediction.objects.create(user=user, match=match, choice="1")
        self.assertTrue(DailyQuest.objects.get(user=user, key="predict").is_done)


class _Req:
    def __init__(self):
        self.session = {}


class ReferralTests(EngagementTestCase):
    def test_attach_and_reward(self):
        inviter, friend = self.make_user("inv"), self.make_user("fr")
        req = _Req()
        referrals.remember(req, referrals.code_for(inviter), Referral.SOURCE_CHALLENGE)
        referrals.attach(req, friend)
        ref = Referral.objects.get(invited=friend)
        self.assertEqual((ref.inviter, ref.source), (inviter, Referral.SOURCE_CHALLENGE))

        referrals.reward_if_due(friend)
        referrals.reward_if_due(friend)  # второй раз — ничего
        self.assertEqual(UserXP.objects.get(user=inviter).total_xp, referrals.REWARD_XP)
        self.assertEqual(UserXP.objects.get(user=friend).total_xp, referrals.REWARD_XP)
        self.assertTrue(UserBadge.objects.filter(user=friend, badge_type="came_with_friend").exists())
        self.assertTrue(UserBadge.objects.filter(user=inviter, badge_type="recruiter_1").exists())

    def test_self_invite_ignored(self):
        user = self.make_user()
        req = _Req()
        referrals.remember(req, referrals.code_for(user), Referral.SOURCE_LINK)
        referrals.attach(req, user)
        self.assertFalse(Referral.objects.exists())

    def test_referral_link_remembers_code_for_anonymous(self):
        inviter = self.make_user()
        code = referrals.code_for(inviter)
        response = self.client.get(reverse('engagement:referral', args=[code]))
        self.assertRedirects(response, reverse('users:register'), fetch_redirect_response=False)
        self.assertEqual(self.client.session[referrals.SESSION_KEY], code)


class FriendLeagueTests(EngagementTestCase):
    def test_standings_count_correct_predictions_after_creation(self):
        owner, friend = self.make_user("o"), self.make_user("f")
        league = friend_leagues.create(owner, "Сектор B")
        self.assertTrue(friend_leagues.join(friend, league))
        self.assertFalse(friend_leagues.join(friend, league))

        old = self.make_match(start=timezone.now() - timedelta(days=5), end=timezone.now() - timedelta(days=5))
        FriendLeague.objects.filter(pk=league.pk).update(created_at=timezone.now() - timedelta(days=1))
        league.refresh_from_db()
        fresh = self.make_match(home_score=2, away_score=1)
        MatchPrediction.objects.bulk_create([
            MatchPrediction(user=owner, match=old, choice="1"),
            MatchPrediction(user=owner, match=fresh, choice="2"),
            MatchPrediction(user=friend, match=fresh, choice="1"),
        ])
        rows = friend_leagues.standings(league)
        self.assertEqual([(r["user"], r["points"], r["place"]) for r in rows], [(friend, 1, 1), (owner, 0, 2)])

    def test_member_limit(self):
        owner = self.make_user()
        league = friend_leagues.create(owner, "Лига")
        with mock.patch.object(friend_leagues, "MAX_MEMBERS", 1):
            with self.assertRaises(friend_leagues.LeagueError):
                friend_leagues.join(self.make_user(), league)

    def test_views(self):
        owner = self.make_user()
        self.client.force_login(owner)
        response = self.client.post(reverse('engagement:friend_leagues'), {"name": "Офис"})
        league = FriendLeague.objects.get(owner=owner)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse('engagement:friend_league', args=[league.invite_code])).status_code, 200)

        self.client.logout()
        guest = self.client.get(reverse('engagement:friend_league', args=[league.invite_code]))
        self.assertEqual(guest.status_code, 200)
        self.assertEqual(self.client.session.get('join_league_after_signup'), league.invite_code)


class FanZoneTests(EngagementTestCase):
    def test_prediction_points_and_top_percent(self):
        from engagement.fanzone import PREDICTION_POINTS, fan_zone

        fans = [self.make_user("fan") for _ in range(4)]
        for fan in fans:
            Follow.objects.create(user=fan, team=self.home)
        match = self.make_match(home_score=1, away_score=0)
        MatchPrediction.objects.create(user=fans[0], match=match, choice="1")
        MatchPrediction.objects.create(user=fans[1], match=match, choice="2")

        data = fan_zone(self.home, fans[0])
        self.assertEqual((data["fans"], data["active"]), (4, 1))
        self.assertEqual(data["leader"]["user"], fans[0])
        self.assertEqual(data["leader"]["points"], PREDICTION_POINTS)
        self.assertEqual((data["me"]["place"], data["me"]["top_percent"]), (1, 25))

    def test_team_page_shows_fan_zone(self):
        user = self.make_user()
        Follow.objects.create(user=user, team=self.home)
        self.client.force_login(user)
        response = self.client.get(reverse('teams:detail', args=[self.home.id]))
        self.assertContains(response, 'id="fan-zone"')
        self.assertContains(response, 'application/ld+json')


class PagesTests(EngagementTestCase):
    def test_season_pass_and_invite(self):
        self.assertEqual(self.client.get(reverse('engagement:season_pass')).status_code, 200)
        self.assertEqual(self.client.get(reverse('engagement:invite')).status_code, 302)
        self.client.force_login(self.make_user())
        self.assertEqual(self.client.get(reverse('engagement:season_pass')).status_code, 200)
        self.assertContains(self.client.get(reverse('engagement:invite')), 'dx-invite-link')

    def test_challenge_banner(self):
        inviter = self.make_user()
        match = self.make_match(status="scheduled", home_score=None, away_score=None,
                                start=timezone.now() + timedelta(days=1))
        url = reverse('engagement:challenge', args=[referrals.code_for(inviter), match.id])
        response = self.client.get(url, follow=True)
        self.assertContains(response, 'dx-challenge-banner')
        self.assertContains(response, inviter.username)

    def test_empty_match_shows_first_vote(self):
        match = self.make_match()
        response = self.client.get(reverse('matches:detail', args=[match.id]))
        self.assertContains(response, 'Будьте первым, кто оценит')

    def test_brag_card(self):
        user = self.make_user()
        url = reverse('engagement:brag_card', args=[user.username, 'predictions'])
        self.assertEqual(self.client.get(url).status_code, 404)
        match = self.make_match(home_score=1, away_score=0)
        MatchPrediction.objects.create(user=user, match=match, choice="1")
        self.assertEqual(self.client.get(url).status_code, 302)
        bad = reverse('engagement:brag_card', args=[user.username, 'fan_top-nope'])
        self.assertEqual(self.client.get(bad).status_code, 404)

    def test_team_players_widget(self):
        response = self.client.get(reverse('engagement:team_players_widget', args=[self.home.id]))
        self.assertEqual(response.status_code, 200)

    @override_settings(STAFF_2FA_ENFORCED=False)
    def test_social_dashboard_page(self):
        admin = User.objects.create_superuser(username="boss", email="boss@test.local", password="x")
        self.client.force_login(admin)
        response = self.client.get(reverse('dashboard:social_content'))
        self.assertContains(response, 'Контент для соцсетей')


class CleanLinkTests(EngagementTestCase):
    def test_glued_text_after_code_redirects(self):
        league = friend_leagues.create(self.make_user(), "Лига")
        url = f"/friends/{league.invite_code}/%20Вступай%20в%20лигу"
        response = self.client.get(url)
        self.assertRedirects(response, reverse('engagement:friend_league', args=[league.invite_code]),
                             fetch_redirect_response=False)


class WeeklyPollTests(EngagementTestCase):
    def _event(self, match, event_type="penalty", minute=80, **extra):
        from events.models import MatchEvent
        return MatchEvent.objects.create(match=match, minute=minute, event_type=event_type, team_side="home", **extra)

    def _round(self):
        m1 = self.make_match(home_score=1, away_score=0)
        m2 = self.make_match(home_score=4, away_score=0)
        Match.objects.filter(pk__in=[m1.pk, m2.pk]).update(tour=5)
        return m1, m2

    def test_picks_most_controversial_episode(self):
        close, rout = self._round()
        self._event(rout, "red_card", minute=20)
        var = self._event(close, "var_check", minute=88, extra_data={"info": "Goal Disallowed", "addition": "Offside"})
        from engagement import polls
        event, match, texts, score = polls.pick_episode()
        self.assertEqual(event, var)
        self.assertEqual(texts[0], "Гол отменили. Правильно?")

        poll = polls.create_poll("episode")
        self.assertIn("88'", poll.context)
        self.assertIsNone(polls.create_poll("episode"))  # не чаще раза в неделю

    def test_vote_counts_once_and_tracks_quest(self):
        close, _ = self._round()
        self._event(close, "penalty")
        from engagement import polls
        poll = polls.create_poll("episode")
        user = self.make_user()
        DailyQuest.objects.create(user=user, date=timezone.localdate(), key="episode_vote", target=1, xp_reward=10)
        self.assertTrue(polls.vote(user, poll, "a"))
        self.assertFalse(polls.vote(user, poll, "b"))
        item = polls.polls_for(user)[0]
        self.assertEqual((item["my"], item["results"]["pct_a"]), ("a", 100))
        self.assertTrue(DailyQuest.objects.get(user=user, key="episode_vote").is_done)

    def test_vote_view_returns_results(self):
        close, _ = self._round()
        self._event(close, "penalty")
        from engagement import polls
        poll = polls.create_poll("episode")
        self.client.force_login(self.make_user())
        response = self.client.post(reverse('engagement:poll_vote', args=[poll.id]), {"choice": "b"})
        self.assertContains(response, "100%")
        self.assertContains(self.client.get(reverse('core:home')), 'id="polls"')

    def test_duel_picks_closest_pair_from_different_clubs(self):
        from aggregates.models import PlayerMatchAggregate
        from players.models import Player
        close, rout = self._round()
        mk = lambda n, team: Player.objects.create(first_name=n, last_name="X", team=team)
        a, b, c = mk("A", self.home), mk("B", self.away), mk("C", self.home)
        for player, match, score in ((a, close, 8.0), (b, rout, 7.9), (c, close, 7.95)):
            PlayerMatchAggregate.objects.create(player=player, match=match, performance_score=score, total_votes=50)
        from engagement import polls
        pair = polls.pick_duel()
        self.assertEqual({pair[0][0], pair[1][0]}, {b, c})

    def test_weekly_task_respects_schedule(self):
        from engagement.tasks import POLL_SCHEDULE
        self.assertEqual(POLL_SCHEDULE, {1: "episode", 2: "duel"})


class QuestPoolTests(EngagementTestCase):
    def test_four_varied_quests(self):
        user = self.make_user()
        items = quests.today_quests(user)["items"]
        self.assertEqual(len(items), quests.QUESTS_PER_DAY)
        self.assertEqual(len({i["key"] for i in items}), quests.QUESTS_PER_DAY)

    def test_visit_quest_via_middleware(self):
        user = self.make_user()
        DailyQuest.objects.create(user=user, date=timezone.localdate(), key="leaderboard", target=1, xp_reward=5)
        self.client.force_login(user)
        self.client.get(reverse('users:leaderboard'))
        self.assertTrue(DailyQuest.objects.get(user=user, key="leaderboard").is_done)

    def test_share_endpoint(self):
        user = self.make_user()
        DailyQuest.objects.create(user=user, date=timezone.localdate(), key="share", target=1, xp_reward=10)
        self.client.force_login(user)
        self.assertEqual(self.client.post(reverse('engagement:share_done')).status_code, 204)
        self.assertTrue(DailyQuest.objects.get(user=user, key="share").is_done)


class EngagementPushTests(EngagementTestCase):
    def test_streak_at_risk_and_league_join(self):
        from engagement.notify import streaks_at_risk
        from notifications.models import Notification
        user = self.make_user()
        DailyStreak.objects.create(user=user, current=5, best=5, last_active_date=timezone.localdate() - timedelta(days=1))
        self.assertEqual(streaks_at_risk(), 1)

        owner, friend = self.make_user("o"), self.make_user("f")
        league = friend_leagues.create(owner, "Лига")
        friend_leagues.join(friend, league)
        self.assertTrue(Notification.objects.filter(user=owner, title__contains="Новый участник").exists())

    def test_new_kinds_follow_user_settings(self):
        from notifications.services import PUSH_KIND_SETTING, _users_allowing
        user = self.make_user()
        settings = user.notification_settings
        settings["push_daily"] = False
        user._notification_settings = settings
        user.save()
        self.assertEqual(PUSH_KIND_SETTING["daily_poll"], "push_daily")
        self.assertEqual(_users_allowing([user.id], "daily_poll"), [])
        self.assertEqual(_users_allowing([user.id], "streak"), [user.id])


class ResetUserActivityTests(EngagementTestCase):
    def test_wipes_activity_keeps_catalog_and_staff(self):
        from django.core.management import call_command
        from players.models import Player
        staff = User.objects.create_superuser(username="boss", email="boss@test.local", password="x")
        UserXP.objects.create(user=staff, total_xp=500, level=4)
        bot = self.make_user("bot")
        match = self.make_match()
        MatchPrediction.objects.create(user=bot, match=match, choice="1")
        MatchPrediction.objects.create(user=staff, match=match, choice="1")
        Player.objects.create(first_name="Иван", last_name="Иванов")

        call_command("reset_user_activity")  # dry-run
        self.assertTrue(User.objects.filter(pk=bot.pk).exists())

        call_command("reset_user_activity", apply=True)
        self.assertFalse(User.objects.filter(pk=bot.pk).exists())
        self.assertTrue(User.objects.filter(pk=staff.pk).exists())
        self.assertFalse(MatchPrediction.objects.exists())
        self.assertEqual(UserXP.objects.get(user=staff).total_xp, 0)
        self.assertTrue(Match.objects.filter(pk=match.pk).exists())
        self.assertEqual(Player.objects.count(), 1)
        self.assertEqual(Team.objects.count(), 2)


class ServiceAccountTests(EngagementTestCase):
    """Суперпользователь тестирует сайт: не влияет на рейтинги, подсчёты и не получает наград."""

    def test_excluded_from_votes_counts_and_badges(self):
        from aggregates.services import excluded_voters_q
        from engagement.rewards import award_badge
        from predictions.services import prediction_counts
        from users.services import check_and_award_badges

        boss = User.objects.create_superuser(username="boss", email="boss@test.local", password="x")
        fan = self.make_user()
        match = self.make_match(status="scheduled", home_score=None, away_score=None,
                                start=timezone.now() + timedelta(days=1))
        MatchPrediction.objects.create(user=boss, match=match, choice="2")
        MatchPrediction.objects.create(user=fan, match=match, choice="1")
        self.assertEqual(prediction_counts(match)["home_pct"], 100)

        self.assertEqual(MatchPrediction.objects.filter(user=boss).exclude(excluded_voters_q(match.id)).count(), 0)
        self.assertEqual(MatchPrediction.objects.filter(user=fan).exclude(excluded_voters_q(match.id)).count(), 1)

        boss.total_evaluations = 10
        self.assertEqual(check_and_award_badges(boss), [])
        self.assertFalse(award_badge(boss, "day_streak_7"))

    def test_not_in_leaderboards(self):
        from core.stats import real_users
        boss = User.objects.create_superuser(username="boss", email="boss@test.local", password="x")
        self.assertNotIn(boss, real_users())
        season.add_season_xp(boss.pk, 500)
        self.client.force_login(boss)
        response = self.client.get(reverse('engagement:season_pass'))
        self.assertEqual(response.context["top"], [])


class StreakLiveTests(EngagementTestCase):
    def yesterday_streak(self, user, current):
        return DailyStreak.objects.create(user=user, current=current, best=current,
                                          last_active_date=timezone.localdate() - timedelta(days=1))

    def test_first_page_of_day_shows_new_streak_and_toast(self):
        user = self.make_user()
        self.yesterday_streak(user, 4)
        self.client.force_login(user)
        response = self.client.get(reverse("core:home"))
        self.assertContains(response, "5 дней")
        self.assertContains(response, "5 дней подряд. Загляните завтра")
        # Второй заход за день — без тоста.
        self.assertNotContains(self.client.get(reverse("core:home")), "Загляните завтра")

    def test_panel_poll_counts_day(self):
        user = self.make_user()
        self.yesterday_streak(user, 2)
        self.client.force_login(user)
        response = self.client.get(reverse("core:personal_panel"), HTTP_HX_REQUEST="true")
        self.assertContains(response, "3 дня")

    def test_milestone_and_lost_streak_notifications(self):
        from notifications.models import Notification

        user = self.make_user()
        self.yesterday_streak(user, 6)
        streaks.touch(user)
        self.assertTrue(Notification.objects.filter(user=user, title__startswith="🔥 7 дней").exists())
        self.assertTrue(Notification.objects.filter(user=user, title="❄️ Новая заморозка").exists())

        other = self.make_user()
        DailyStreak.objects.create(user=other, current=9, best=9, last_active_date=timezone.localdate() - timedelta(days=5))
        streak = streaks.touch(other)
        self.assertEqual(streak.lost, 9)
        self.assertIn("прервалась", streaks.toast(streak))
        self.assertTrue(Notification.objects.filter(user=other, title="Серия прервалась").exists())

    def test_live_version_changes_with_personal_progress(self):
        user = self.make_user()
        self.client.force_login(user)
        url = reverse("core:live_version")
        before = self.client.get(url).json()["v"]
        self.assertEqual(before, self.client.get(url).json()["v"])
        with self.captureOnCommitCallbacks(execute=True):
            UserXP.objects.get_or_create(user=user)[0].add_xp(5)
        self.assertNotEqual(before, self.client.get(url).json()["v"])

    def test_public_profile_shows_progress_and_card_preview(self):
        user = self.make_user(is_profile_public=True)
        self.yesterday_streak(user, 5)
        streaks.touch(user)
        SeasonPass.objects.create(user=user, season=self.season, xp=250)
        url = reverse("users:public_profile", args=[user.username])
        response = self.client.get(url + "?card=season")
        self.assertContains(response, "дней подряд · рекорд 6")
        self.assertContains(response, "уровень пропуска")
        self.assertContains(response, reverse("engagement:brag_card", args=[user.username, "season"]))
        self.assertContains(response, "dx-brag--season is-highlight")
        # Карточки нет — обычное превью.
        self.assertNotContains(self.client.get(url + "?card=predictions"), "share/brag/")
