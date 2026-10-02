# adminbot/ask.py
"""Вопросы боту свободным текстом: Claude отвечает через read-only инструменты по агрегатам.
Личные данные пользователей в инструменты не попадают."""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

API = "https://api.anthropic.com/v1/messages"
MAX_STEPS = 6
SYSTEM = (
    "Ты аналитик футбольной платформы DOPX (рейтинги игроков КПЛ по оценкам болельщиков). "
    "Отвечай сотруднику по-русски, коротко (до 8 строк), простым текстом без Markdown. "
    "Данные бери только из инструментов; если данных нет — так и скажи. Рейтинг — шкала 0–10. "
    "Сегодня {today}."
)


def enabled() -> bool:
    """Вопросы текстом тратят баланс API — включаются отдельно (переключатель «Вопросы боту текстом»)."""
    from .flags import get

    from core.safe_mode import allowed

    return bool(settings.ANTHROPIC_API_KEY and get("bot_ai_chat") and allowed("ai"))


def _season():
    from engagement.season import current_season

    return current_season()


def _d(value: str | None, default: date) -> date:
    try:
        return date.fromisoformat(value) if value else default
    except ValueError:
        return default


# ---------------- Инструменты
def t_find_player(name: str) -> list[dict]:
    from aggregates.models import PlayerMatchAggregate
    from players.models import Player

    parts = name.split()
    q = Q()
    for p in parts:
        q &= Q(first_name__icontains=p) | Q(last_name__icontains=p)
    season = _season()
    out = []
    for pl in Player.objects.filter(q).select_related("team")[:5]:
        aggs = PlayerMatchAggregate.objects.filter(player=pl, total_votes__gt=0)
        if season:
            aggs = aggs.filter(match__season=season)
        s = aggs.aggregate(matches=Count("id"), avg=Avg("performance_score"), votes=Sum("total_votes"))
        out.append({"id": str(pl.pk), "name": pl.full_name, "team": pl.team.name if pl.team_id else "",
                    "season_matches_rated": s["matches"], "season_avg_rating": round(s["avg"], 2) if s["avg"] else None,
                    "season_votes": s["votes"] or 0})
    return out


def t_player_matches(player_id: str, limit: int = 8) -> list[dict]:
    from aggregates.models import PlayerMatchAggregate

    rows = (PlayerMatchAggregate.objects.filter(player_id=player_id, total_votes__gt=0)
            .select_related("match__home_team", "match__away_team").order_by("-match__start_time")[:min(limit, 20)])
    return [{"date": f"{timezone.localtime(a.match.start_time):%d.%m.%Y}", "tour": a.match.tour,
             "match": f"{a.match.home_team.name} {a.match.home_score}:{a.match.away_score} {a.match.away_team.name}",
             "rating": round(a.performance_score, 2), "votes": a.total_votes} for a in rows]


def t_find_matches(team: str = "", tour: int | None = None, date_from: str = "", date_to: str = "") -> list[dict]:
    from aggregates.models import MatchAggregate, RefereeMatchAggregate
    from matches.models import Match

    qs = Match.objects.select_related("home_team", "away_team", "referee").order_by("start_time")
    season = _season()
    if tour:
        qs = qs.filter(tour=tour, **({"season": season} if season else {}))
    if team:
        qs = qs.filter(Q(home_team__name__icontains=team) | Q(away_team__name__icontains=team))
    if date_from or date_to or not (tour or team):
        today = timezone.localdate()
        qs = qs.filter(start_time__date__gte=_d(date_from, today - timedelta(days=7)), start_time__date__lte=_d(date_to, today + timedelta(days=7)))
    out = []
    for m in qs[:20]:
        agg = MatchAggregate.objects.filter(match=m).first()
        ref = RefereeMatchAggregate.objects.filter(match=m).first()
        gap = abs(ref.home_fans_avg - ref.away_fans_avg) if ref and ref.home_fans_avg is not None and ref.away_fans_avg is not None else None
        out.append({"date": f"{timezone.localtime(m.start_time):%d.%m.%Y %H:%M}", "tour": m.tour, "status": m.status,
                    "match": f"{m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}",
                    "votes": agg.total_votes if agg else 0, "drama_index": round(agg.drama_index, 2) if agg else None,
                    "fairness": round(agg.avg_fairness, 2) if agg else None,
                    "referee": m.referee.full_name if m.referee_id else "",
                    "referee_fans_gap": round(gap, 2) if gap is not None else None})
    return out


def t_top_players(tour: int | None = None, limit: int = 5, worst: bool = False) -> list[dict]:
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display

    season = _season()
    qs = PlayerMatchAggregate.objects.filter(total_votes__gte=min_votes_for_display()).select_related("player__team", "match")
    if season:
        qs = qs.filter(match__season=season)
    if tour:
        qs = qs.filter(match__tour=tour)
        rows = qs.order_by("performance_score" if worst else "-performance_score")[:min(limit, 15)]
        return [{"name": a.player.full_name, "team": a.player.team.name if a.player.team_id else "",
                 "rating": round(a.performance_score, 2), "votes": a.total_votes} for a in rows]
    rows = (qs.values("player__first_name", "player__last_name").annotate(avg=Avg("performance_score"), n=Count("id"))
            .filter(n__gte=3).order_by("avg" if worst else "-avg")[:min(limit, 15)])
    return [{"name": f"{r['player__first_name']} {r['player__last_name']}", "avg_rating": round(r["avg"], 2), "matches": r["n"]} for r in rows]


def t_round_summary(tour: int | None = None) -> dict:
    from round_squad.models import RoundBestXI

    qs = RoundBestXI.objects.select_related("most_dramatic_match__home_team", "most_dramatic_match__away_team").order_by("-season__year", "-tour")
    if tour:
        qs = qs.filter(tour=tour)
    r = qs.first()
    if not r:
        return {"error": "тур не найден"}
    m = r.most_dramatic_match
    return {"tour": r.tour, "final": r.is_final, "player_of_round": r.player_of_round_name,
            "player_of_round_rating": r.player_of_round_score, "player_of_round_votes": r.player_of_round_votes,
            "most_dramatic_match": f"{m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}" if m else None,
            "squad": [s.occupant_name for s in r.slots.order_by("order") if s.occupant_name]}


def t_site_stats(date_from: str = "", date_to: str = "") -> dict:
    from .reports import metrics

    today = timezone.localdate()
    start, end = _d(date_from, today - timedelta(days=6)), _d(date_to, today)
    tz = timezone.get_current_timezone()
    from datetime import datetime, time

    data = metrics(datetime.combine(start, time.min, tz), datetime.combine(end + timedelta(days=1), time.min, tz))
    return {"period": f"{start:%d.%m.%Y}–{end:%d.%m.%Y}", **data}


TOOLS = {
    "find_player": (t_find_player, None, "Найти игрока по имени/фамилии: команда и статистика сезона по оценкам болельщиков.",
                    {"name": {"type": "string"}}, ["name"]),
    "player_matches": (t_player_matches, None, "Последние оценки игрока по матчам (id из find_player).",
                       {"player_id": {"type": "string"}, "limit": {"type": "integer"}}, ["player_id"]),
    "find_matches": (t_find_matches, None, "Матчи: по команде, туру или датам (YYYY-MM-DD). Голоса, драма, справедливость, судья, разрыв оценок судьи фанатами двух команд (спорность).",
                     {"team": {"type": "string"}, "tour": {"type": "integer"}, "date_from": {"type": "string"}, "date_to": {"type": "string"}}, []),
    "top_players": (t_top_players, None, "Лучшие (или худшие, worst=true) игроки тура или сезона текущего сезона.",
                    {"tour": {"type": "integer"}, "limit": {"type": "integer"}, "worst": {"type": "boolean"}}, []),
    "round_summary": (t_round_summary, None, "Итоги тура: игрок тура, самый драматичный матч, сборная тура. Без tour — последний.",
                      {"tour": {"type": "integer"}}, []),
    "site_stats": (t_site_stats, "overview", "Статистика платформы за период: регистрации, активные, визиты, оценки, прогнозы.",
                   {"date_from": {"type": "string"}, "date_to": {"type": "string"}}, []),
}


def _tools_for(user):
    from .handlers import can

    return {name: spec for name, spec in TOOLS.items() if spec[1] is None or can(user, spec[1])}


def answer(user, question: str) -> str:
    import anthropic

    tools = _tools_for(user)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=60.0, max_retries=2)
    request = {
        "model": settings.ADMIN_BOT_AI_MODEL, "max_tokens": 4000, "output_config": {"effort": "low"},
        "betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default",
        "system": SYSTEM.format(today=timezone.localdate().isoformat()),
        "tools": [{"name": n, "description": s[2], "input_schema": {"type": "object", "properties": s[3], "required": s[4]}}
                  for n, s in tools.items()],
    }
    messages = [{"role": "user", "content": question[:1000]}]
    try:
        for _ in range(MAX_STEPS):
            response = client.beta.messages.create(messages=messages, **request)
            if response.stop_reason != "tool_use":
                text = "\n".join(b.text for b in response.content if b.type == "text").strip()
                return text or "Не нашёл ответа."
            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                spec = tools.get(block.name)
                try:
                    out = spec[0](**(block.input or {})) if spec else {"error": "нет такого инструмента"}
                except Exception as e:   # неверные аргументы — сообщаем модели, а не падаем
                    out = {"error": str(e)[:200]}
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)[:12000]})
            messages.append({"role": "user", "content": results})
    except anthropic.APIError as e:
        logger.warning("adminbot ask: Claude API: %s", e)
        return "Claude сейчас недоступен (ключ, баланс или сеть), попробуйте позже."
    return "Вопрос оказался слишком сложным — уточните, пожалуйста."
