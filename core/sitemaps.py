# core/sitemaps.py
from __future__ import annotations

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from coaches.models import Coach
from matches.models import Match
from players.models import Player
from teams.models import Team


class MatchSitemap(Sitemap):
    """
    Только завершённые матчи: до завершения на странице ещё нет контента
    (оценки открываются постфактум) — индексировать пустую страницу это
    сигнал низкого качества для Google/Яндекс.
    """
    changefreq = "weekly"
    priority = 0.7
    protocol = "https"

    def items(self):
        return Match.objects.filter(status="finished").select_related("home_team", "away_team")

    def location(self, obj: Match) -> str:
        return reverse("matches:detail", args=[obj.pk])

    def lastmod(self, obj: Match):
        return obj.updated_at


class PlayerSitemap(Sitemap):
    changefreq, priority, protocol = "weekly", 0.6, "https"

    def items(self):
        return Player.objects.filter(is_active=True)

    def location(self, obj: Player) -> str:
        return reverse("players:detail", args=[obj.pk])

    def lastmod(self, obj: Player):
        return obj.updated_at


class TeamSitemap(Sitemap):
    changefreq, priority, protocol = "monthly", 0.5, "https"

    def items(self):
        return Team.objects.filter(is_active=True)

    def location(self, obj: Team) -> str:
        return reverse("teams:detail", args=[obj.pk])


class CoachSitemap(Sitemap):
    changefreq, priority, protocol = "monthly", 0.4, "https"

    def items(self):
        return Coach.objects.filter(is_active=True)

    def location(self, obj: Coach) -> str:
        return reverse("coaches:detail", args=[obj.pk])


class StaticViewSitemap(Sitemap):
    changefreq, priority, protocol = "monthly", 0.3, "https"

    def items(self):
        return ["core:home", "core:rules", "core:anti_fraud", "users:leaderboard"]

    def location(self, item: str) -> str:
        return reverse(item)
