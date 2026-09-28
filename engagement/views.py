# engagement/views.py
"""Страницы вовлечения: сезонный пропуск, лиги с друзьями, приглашения, карточки, виджет."""
from __future__ import annotations

import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.files.storage import default_storage
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_POST

from . import friend_leagues, quests, referrals, season, streaks
from .models import FriendLeague, Referral, ReferralCode, SeasonPass
from .quests import track

SEASON_TOP_SIZE = 10


def season_pass(request):
    """Сезонный пропуск: 30 уровней, награды, сезонный рейтинг болельщиков."""
    overview = season.overview(request.user)
    top, my_place = [], None
    if overview:
        passes = SeasonPass.objects.filter(season=overview["season"], xp__gt=0, user__is_active=True).select_related("user")
        top = [{"place": i, "user": p.user, "xp": p.xp, "level": season.level_for(p.xp)}
               for i, p in enumerate(passes.order_by("-xp", "created_at")[:SEASON_TOP_SIZE], start=1)]
        if request.user.is_authenticated and overview["xp"]:
            my_place = passes.filter(xp__gt=overview["xp"]).count() + 1
    # Ближайшая награда и участники сезона — для карточки «Ваш сезон».
    next_reward = None
    participants = 0
    if overview:
        participants = passes.count()
        lvl = next((l for l in sorted(season.REWARDS) if l > overview["level"]), None)
        if lvl:
            next_reward = {"level": lvl, "xp_left": (lvl - 1) * season.XP_PER_LEVEL - overview["xp"], **season.REWARDS[lvl]}
    track_rewards = []
    if overview:
        for level in range(1, season.LEVELS + 1):
            reward = season.REWARDS.get(level)
            track_rewards.append({
                "level": level, "reward": reward, "reached": overview["level"] >= level,
                "claimed": level in overview["claimed"],
            })
    share_url = share_text = ""
    if overview and request.user.is_authenticated and overview["xp"]:
        # Ссылка-приглашение: друг, пришедший по ней, приносит обоим XP.
        share_url = request.build_absolute_uri(reverse("engagement:referral", args=[referrals.code_for(request.user)]) + "?to=season")
        share_text = (f"У меня {overview['level']}-й уровень сезонного пропуска DOPX "
                      f"({overview['xp']} XP за сезон). Догонишь?")
    return render(request, "engagement/season_pass.html", {
        "page_title": "Сезонный пропуск — DOPX", "overview": overview, "levels": track_rewards,
        "top": top, "my_place": my_place, "xp_per_level": season.XP_PER_LEVEL,
        "share_url": share_url, "share_text": share_text,
        "next_reward": next_reward, "participants": participants,
        "my_outside_top": bool(my_place and my_place > SEASON_TOP_SIZE),
        "xp_rules": {"quests_bonus": quests.ALL_DONE_XP, "referral": referrals.REWARD_XP,
                     "max_streak": max(streaks.MILESTONE_XP.values())},
    })


@login_required
def friend_leagues_view(request):
    """Мои лиги прогнозистов + создание."""
    if request.method == "POST":
        try:
            league = friend_leagues.create(request.user, request.POST.get("name", ""))
        except friend_leagues.LeagueError as e:
            messages.error(request, str(e))
            return redirect("engagement:friend_leagues")
        track(request.user, "create_league")
        messages.success(request, "Лига создана. Отправьте ссылку друзьям.")
        return redirect("engagement:friend_league", code=league.invite_code)
    leagues = []
    for league in FriendLeague.objects.filter(memberships__user=request.user).order_by("-created_at"):
        rows = friend_leagues.standings(league)
        mine = next((r for r in rows if r["user"].pk == request.user.pk), None)
        leagues.append({"league": league, "members": len(rows), "my_place": mine["place"] if mine else None, "leader": rows[0] if rows else None})
    return render(request, "engagement/friend_leagues.html", {"page_title": "Лиги с друзьями — DOPX", "leagues": leagues})


def friend_league_view(request, code):
    """Таблица лиги; для не участников — приглашение вступить."""
    league = get_object_or_404(FriendLeague.objects.select_related("owner", "season"), invite_code=code)
    is_member = request.user.is_authenticated and league.memberships.filter(user=request.user).exists()
    if is_member:
        track(request.user, "league")
    elif not request.user.is_authenticated:
        # Регистрация по ссылке лиги — приглашение от владельца.
        referrals.remember(request, referrals.code_for(league.owner), Referral.SOURCE_LEAGUE)
        request.session["join_league_after_signup"] = league.invite_code
    invite_url = request.build_absolute_uri(reverse("engagement:friend_league", args=[league.invite_code]))
    return render(request, "engagement/friend_league_detail.html", {
        "page_title": f"{league.name} — лига прогнозистов DOPX", "league": league, "is_member": is_member,
        "rows": friend_leagues.standings(league), "invite_url": invite_url,
        "share_text": f"Вступай в мою лигу прогнозистов «{league.name}» на DOPX. Посмотрим, кто лучше угадывает матчи.",
    })


@login_required
@require_POST
def friend_league_join(request, code):
    league = get_object_or_404(FriendLeague, invite_code=code)
    try:
        if friend_leagues.join(request.user, league):
            messages.success(request, f"Вы в лиге «{league.name}». Ставьте прогнозы на матчи, очки считаются сами.")
    except friend_leagues.LeagueError as e:
        messages.error(request, str(e))
    return redirect("engagement:friend_league", code=code)


@login_required
@require_POST
def friend_league_leave(request, code):
    league = get_object_or_404(FriendLeague, invite_code=code)
    if league.owner_id == request.user.pk:
        messages.error(request, "Создатель не может покинуть лигу.")
        return redirect("engagement:friend_league", code=code)
    league.memberships.filter(user=request.user).delete()
    messages.success(request, f"Вы вышли из лиги «{league.name}».")
    return redirect("engagement:friend_leagues")


@login_required
def invite(request):
    """Моя ссылка-приглашение и её результат."""
    code = referrals.code_for(request.user)
    invite_url = request.build_absolute_uri(reverse("engagement:referral", args=[code]))
    return render(request, "engagement/invite.html", {
        "page_title": "Пригласить друзей — DOPX", "invite_url": invite_url, "stats": referrals.stats(request.user),
        "reward_xp": referrals.REWARD_XP,
        "share_text": "Оцениваю матчи КПЛ и соревнуюсь в прогнозах на DOPX. Заходи, по моей ссылке дадут бонус.",
    })


def referral(request, code):
    """Вход по ссылке-приглашению."""
    get_object_or_404(ReferralCode, code=code)
    # ?to= — куда вести уже зарегистрированного (только из белого списка).
    target = {"season": "engagement:season_pass"}.get(request.GET.get("to"), "core:home")
    if request.user.is_authenticated:
        return redirect(target)
    referrals.remember(request, code, Referral.SOURCE_LINK)
    return redirect("users:register")


def clean_link(request, code, tail, name):
    """Ссылка с мусором после кода — на чистый адрес."""
    return redirect(name, code=code.strip())


def challenge(request, code, match_id):
    """«Спорим, мой прогноз точнее?» — ведёт на матч с плашкой вызова."""
    ref = get_object_or_404(ReferralCode.objects.select_related("user"), code=code)
    if not request.user.is_authenticated:
        referrals.remember(request, code, Referral.SOURCE_CHALLENGE)
    return redirect(f"{reverse('matches:detail', args=[match_id])}?challenge={ref.user.username}")


def brag_card(request, username, kind):
    """PNG «похвастаться». Числа берём из БД, а не из URL."""
    from core.services.share_cards import build_brag_share_card
    from engagement.fanzone import fan_zone
    from engagement.streaks import state as streak_state
    from teams.models import Team
    from users.models import User

    user = get_object_or_404(User, username=username, is_profile_public=True)
    if kind == "season":
        data = season.overview(user)
        if not data or data["xp"] <= 0:
            raise Http404
        params = dict(number_text=str(data["level"]), label_line1="уровень сезонного пропуска",
                      label_line2=f"сезон {data['season'].year}", eyebrow="СЕЗОН DOPX")
    elif kind == "predictions":
        from predictions.services import correct_predictions_count
        hits = correct_predictions_count(user)
        if hits < 1:
            raise Http404
        params = dict(number_text=str(hits), label_line1="раз прогноз сбылся",
                      label_line2="прогнозы 1X2 на DOPX", eyebrow="ПРОГНОЗИСТ DOPX")
    elif kind == "day_streak":
        data = streak_state(user)
        if not data or data["current"] < 2:
            raise Http404
        params = dict(number_text=str(data["current"]), label_line1="дней подряд", label_line2="с DOPX", eyebrow="СЕРИЯ ДНЕЙ")
    elif kind.startswith("fan_top-"):
        try:
            team_id = uuid.UUID(kind.split("-", 1)[1])
        except ValueError:
            raise Http404
        team = get_object_or_404(Team, pk=team_id)
        me = fan_zone(team, user)["me"]
        if not me or not me["top_percent"]:
            raise Http404
        params = dict(number_text=f"{me['top_percent']}%", label_line1="лучших болельщиков",
                      label_line2=team.name, eyebrow="ФАН-ЗОНА КЛУБА")
        kind = "fan_top"
    else:
        raise Http404
    path = build_brag_share_card(username=user.username, kind=kind, **params)
    return redirect(default_storage.url(path))


@xframe_options_exempt
def team_players_widget(request, team_id):
    """Встраиваемый виджет: лучшие игроки клуба по оценкам болельщиков DOPX."""
    from aggregates.services import published_q, vote_weighted_avg
    from django.db.models import Count, Q
    from players.models import Player
    from teams.models import Team

    team = get_object_or_404(Team, pk=team_id)
    counted = published_q("match_aggregates__match__") & Q(match_aggregates__total_votes__gte=3)
    players = (
        Player.objects.filter(team=team, is_active=True)
        .annotate(rating=vote_weighted_avg("match_aggregates__performance_score", "match_aggregates__total_votes", filter=counted),
                  matches=Count("match_aggregates", filter=counted))
        .filter(rating__isnull=False, matches__gte=2).order_by("-rating")[:10]
    )
    return render(request, "widgets/team_players.html", {"team": team, "players": players,
                                                         "site_url": request.build_absolute_uri("/")})


@require_POST
def poll_vote(request, poll_id):
    """Голос в опросе недели — возвращает карточку с результатами (HTMX)."""
    from . import polls
    from .models import DailyPoll

    poll = get_object_or_404(DailyPoll, pk=poll_id)
    if request.user.is_authenticated:
        polls.vote(request.user, poll, request.POST.get("choice", ""))
    item = next((i for i in polls.polls_for(request.user) if i["poll"].id == poll.id), None)
    if item is None:  # закрыт — показываем итог
        from .models import DailyPollVote
        my = DailyPollVote.objects.filter(poll=poll, user=request.user).values_list("choice", flat=True).first() \
            if request.user.is_authenticated else None
        item = {"poll": poll, "my": my, "results": polls.results(poll)}
    return render(request, "engagement/_poll_card.html", {"item": item})


@require_POST
def share_done(request):
    """share.js сообщает о нажатии «Поделиться» — засчитываем задание дня."""
    from django.http import HttpResponse

    if request.user.is_authenticated:
        track(request.user, "share")
    return HttpResponse(status=204)
