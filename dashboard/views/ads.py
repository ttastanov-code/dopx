# dashboard/views/ads.py
"""Реклама и виджеты."""
from __future__ import annotations

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.urls import reverse

from core.models import get_setting


# Реклама и виджеты: embed-виджеты и баннеры/рефералки на одной странице.

def _ads_stats_context() -> dict:
    """Статистика за период (виджеты, баннеры, рефералки) — для страницы и HTMX-поллинга."""
    from partners.selectors import (
        banner_totals,
        partner_referral_totals,
        top_banners,
        top_partners_by_referral_visits,
        top_widget_entities,
        widget_embed_totals,
    )
    from partners.models import Banner, Partner
    from players.models import Player
    from teams.models import Team

    # Период и размер топов — из настроек платформы.
    window_days = get_setting("ads_stats_window_days", 30)
    top_limit = get_setting("ads_top_items_limit", 10)

    top_players_raw = top_widget_entities("player", days=window_days, limit=top_limit)
    top_teams_raw = top_widget_entities("team", days=window_days, limit=top_limit)

    players_by_id = {
        str(p.id): p for p in Player.objects.filter(id__in=[r["entity_id"] for r in top_players_raw])
    }
    teams_by_id = {
        str(t.id): t for t in Team.objects.filter(id__in=[r["entity_id"] for r in top_teams_raw])
    }

    top_banners_raw = top_banners(days=window_days, limit=top_limit)
    banners_by_id = {
        str(b.id): b for b in Banner.objects.select_related("partner").filter(id__in=[r["banner_id"] for r in top_banners_raw])
    }

    top_partners_raw = top_partners_by_referral_visits(days=window_days, limit=top_limit)
    partners_by_slug = {
        p.slug: p for p in Partner.objects.filter(slug__in=[r["partner_slug"] for r in top_partners_raw])
    }

    return {
        "top_players": [
            {"entity": players_by_id[r["entity_id"]], "views": r["views"]}
            for r in top_players_raw if r["entity_id"] in players_by_id
        ],
        "top_teams": [
            {"entity": teams_by_id[r["entity_id"]], "views": r["views"]}
            for r in top_teams_raw if r["entity_id"] in teams_by_id
        ],
        "widget_totals": widget_embed_totals(days=window_days),
        "banner_totals": banner_totals(days=window_days),
        "top_banners": [
            {"banner": banners_by_id[r["banner_id"]], "impressions": r["impressions"], "clicks": r["clicks"], "ctr_percent": r["ctr_percent"]}
            for r in top_banners_raw if r["banner_id"] in banners_by_id
        ],
        "referral_visits_total": partner_referral_totals(days=window_days),
        "top_partners": [
            {"partner": partners_by_slug[r["partner_slug"]], "visits": r["visits"]}
            for r in top_partners_raw if r["partner_slug"] in partners_by_slug
        ],
    }


@staff_member_required
def ads(request):
    """/staff/dashboard/ads/. q_player/q_team — отдельный поиск игрока и команды
    для генератора embed-кода.
    """
    from core.utils import normalize_kz
    from players.models import Player
    from teams.models import Team

    q_player = request.GET.get("q_player", "").strip()
    q_team = request.GET.get("q_team", "").strip()
    player_id = request.GET.get("player_id", "")
    team_id = request.GET.get("team_id", "")

    # Поиск через normalize_kz (казахские буквы). Размер выдачи — из настроек.
    search_limit = get_setting("ads_search_results_limit", 10)

    if q_player:
        normalized_q = normalize_kz(q_player)
        player_results = [
            p for p in Player.objects.select_related("team").only("id", "first_name", "last_name", "team")
            if normalized_q in normalize_kz(f"{p.first_name} {p.last_name}")
        ][:search_limit]
    else:
        player_results = []

    if q_team:
        normalized_q = normalize_kz(q_team)
        team_results = [
            t for t in Team.objects.only("id", "name")
            if normalized_q in normalize_kz(t.name)
        ][:search_limit]
    else:
        team_results = []

    # Без поиска — превью на произвольном игроке/команде с данными.
    preview_player = None
    if player_id:
        preview_player = next((p for p in player_results if str(p.id) == player_id), None)
    if not preview_player:
        preview_player = player_results[0] if player_results else Player.objects.select_related("team").order_by("?").first()

    preview_team = None
    if team_id:
        preview_team = next((t for t in team_results if str(t.id) == team_id), None)
    if not preview_team:
        preview_team = team_results[0] if team_results else Team.objects.filter(is_active=True).order_by("?").first()

    def _embed_code(url: str, title: str, width: int = 320, height: int = 180) -> str:
        return (
            f'<iframe src="{url}" width="{width}" height="{height}" '
            f'style="border:none;border-radius:12px;overflow:hidden" title="{title}"></iframe>'
        )

    player_embed = None
    if preview_player:
        url = request.build_absolute_uri(reverse("players:widget", args=[preview_player.id]))
        player_embed = _embed_code(url, f"Рейтинг {preview_player.first_name} {preview_player.last_name} на DOPX")

    team_embed = None
    if preview_team:
        url = request.build_absolute_uri(reverse("teams:widget", args=[preview_team.id]))
        team_embed = _embed_code(url, f"Рейтинг {preview_team.name} на DOPX")

    standings_url = request.build_absolute_uri(reverse("core:standings_widget"))
    standings_embed = _embed_code(standings_url, "Турнирная таблица КПЛ на DOPX", width=340, height=360)

    # Виджет сборной сезона — всегда активный сезон.
    best_xi_url = request.build_absolute_uri(reverse("season_squad:widget"))
    best_xi_embed = _embed_code(best_xi_url, "Сборная DOPX сезона на DOPX", width=320, height=420)

    # Виджет лучших тура — активный сезон, последний завершённый тур.
    round_url = request.build_absolute_uri(reverse("round_squad:round_widget"))
    round_embed = _embed_code(round_url, "DOPX Лучшие тура", width=320, height=420)

    context = {
        "page_title": "Реклама и виджеты — DOPX Staff",
        "active_tab": "ads",
        "q_player": q_player,
        "q_team": q_team,
        "player_results": player_results,
        "team_results": team_results,
        "preview_player": preview_player,
        "preview_team": preview_team,
        "player_embed": player_embed,
        "team_embed": team_embed,
        "standings_embed": standings_embed,
        "best_xi_embed": best_xi_embed,
        "round_embed": round_embed,
        **_ads_stats_context(),
    }
    return render(request, "dashboard/ads.html", context)


@staff_member_required
def ads_stats_partial(request):
    """Блок статистики ads() для HTMX-поллинга (поиск и превью не обновляются)."""
    return render(request, "dashboard/_ads_stats_content.html", _ads_stats_context())
