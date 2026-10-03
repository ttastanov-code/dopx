# adminbot/stories.py
"""Мини-статьи для канала: факты из базы (только проверенное) + шаблон на случай, если Claude недоступен.
Тексты пишет writer.write: Claude по фактам или шаблон отсюда."""
from __future__ import annotations

import random
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

GOAL_TYPES = ("goal", "penalty", "own_goal")


def _r(x):
    """Одна цифра после запятой; целое — без «.0»."""
    if x is None:
        return None
    x = round(float(x), 1)
    return int(x) if x == int(x) else x


def _score1(x) -> str:
    """Оценка с одной цифрой после запятой: 3 → «3.0»."""
    return f"{float(x):.1f}"


def _rng(key) -> random.Random:
    return random.Random(str(key))


def _esc(s) -> str:
    import html

    return html.escape(str(s), quote=False)


# ---------------- Разбор матча
def _events(match):
    from events.models import MatchEvent

    return list(MatchEvent.objects.filter(match=match).select_related("player", "assist_player").order_by("minute", "added_time"))


def _team_name(match, side: str) -> str:
    return match.home_team.name if side == "home" else match.away_team.name


def _stats(match) -> dict:
    from matches.models import MatchTeamStatistics

    rows = {s.team_id: s for s in MatchTeamStatistics.objects.filter(match=match)}
    home, away = rows.get(match.home_team_id), rows.get(match.away_team_id)
    if not home or not away:
        return {}
    out = {}
    for label, field in (("xG", "xg"), ("владение_%", "possession_percent"), ("удары", "shots"),
                         ("удары_в_створ", "shots_on_goal"), ("угловые", "corners")):
        h, a = getattr(home, field), getattr(away, field)
        if h is not None and a is not None:
            out[label] = [_r(h), _r(a)]
    return out


def _player(a, match) -> dict:
    team = match.home_team.name if a.player.team_id == match.home_team_id else (
        match.away_team.name if a.player.team_id == match.away_team_id else "")
    out = {"игрок": a.player.full_name, "команда": team, "оценка": _r(a.performance_score), "голосов": a.total_votes}
    if a.own_fans_avg is not None and a.rival_fans_avg is not None:
        out.update(свои_фанаты=_r(a.own_fans_avg), фанаты_соперника=_r(a.rival_fans_avg))
    return out


def match_facts(match) -> dict:
    from aggregates.models import MatchAggregate, PlayerMatchAggregate, RefereeMatchAggregate
    from aggregates.services import min_votes_for_display
    from engagement.models import ExpertTake
    from evaluations.models import EvaluationSession
    from matches.services import compute_sensation_index, describe_key_moment
    from predictions.services import prediction_counts
    from teams.services import compute_match_table_impact_positions

    events = _events(match)
    facts: dict = {"матч": {"хозяева": match.home_team.name, "гости": match.away_team.name,
                            "счёт": f"{match.home_score}:{match.away_score}", "тур": match.tour,
                            "дата": f"{timezone.localtime(match.start_time):%d.%m}"}}
    goals = []
    for e in events:
        if e.event_type in GOAL_TYPES:
            goals.append({"минута": e.display_minute, "команда": _team_name(match, e.team_side),
                          "автор": e.player_display_name or "", "тип": e.get_event_type_display(),
                          "счёт_после": (e.score_after or "").replace("-", ":")})
    if goals:
        facts["голы"] = goals
    reds = [{"минута": e.display_minute, "команда": _team_name(match, e.team_side), "игрок": e.player_display_name or ""}
            for e in events if e.event_type == "red_card"]
    if reds:
        facts["удаления"] = reds
    key = describe_key_moment(match, events)
    if key:
        facts["главный_момент"] = key
    stats = _stats(match)
    if stats:
        facts["статистика_хозяева_гости"] = stats
    facts["оценок_болельщиков"] = EvaluationSession.objects.filter(match=match, status="completed").count()
    aggs = list(PlayerMatchAggregate.objects.filter(match=match, total_votes__gte=min_votes_for_display())
                .select_related("player").order_by("-performance_score"))
    if aggs:
        facts["лучшие"] = [_player(a, match) for a in aggs[:3]]
    if len(aggs) > 3:
        facts["антигерой"] = _player(aggs[-1], match)
    split = max((a for a in aggs if a.own_fans_avg is not None and a.rival_fans_avg is not None),
                key=lambda a: abs(a.own_fans_avg - a.rival_fans_avg), default=None)
    if split and abs(split.own_fans_avg - split.rival_fans_avg) >= 1.5:
        facts["раскол_трибун"] = {**_player(split, match), "разрыв": _r(abs(split.own_fans_avg - split.rival_fans_avg))}
    ref = RefereeMatchAggregate.objects.filter(match=match).select_related("referee").first()
    if ref and ref.total_votes >= min_votes_for_display():
        facts["судья"] = {"имя": ref.referee.full_name, "оценка": _r(ref.performance_score)}
        if ref.home_fans_avg is not None and ref.away_fans_avg is not None:
            facts["судья"].update(фанаты_хозяев=_r(ref.home_fans_avg), фанаты_гостей=_r(ref.away_fans_avg),
                                  разрыв=_r(abs(ref.home_fans_avg - ref.away_fans_avg)))
    counts = prediction_counts(match)
    if counts["total"] >= 5:
        facts["прогнозы_до_матча"] = {"за_хозяев_%": counts["home_pct"], "ничья_%": counts["draw_pct"],
                                      "за_гостей_%": counts["away_pct"], "всего": counts["total"]}
        upset = compute_sensation_index(match, counts)
        if upset:
            facts["сенсация"] = f"{upset}% болельщиков ждали другого исхода"
    agg = MatchAggregate.objects.filter(match=match).first()
    if agg and agg.total_votes:
        facts["индекс_драмы_из_100"] = _r(agg.drama_index)
    try:
        before, after = compute_match_table_impact_positions(match)
        table = {}
        for team in (match.home_team, match.away_team):
            b, a = before.get(team.pk), after.get(team.pk)
            if b and a and b != a:
                table[team.name] = {"было_место": b, "стало_место": a}
        if table:
            facts["таблица"] = table
    except Exception:
        pass
    take = ExpertTake.objects.filter(match=match, is_published=True).select_related("expert").order_by("-created_at").first()
    if take:
        facts["эксперт"] = {"имя": take.display_name, "кто": take.display_title,
                            "цитата": take.headline or take.text[:240]}
    return facts


def review_template(match, facts: dict) -> str:
    rng = _rng(f"review:{match.pk}")
    h, a = match.home_team.name, match.away_team.name
    hs, as_ = match.home_score, match.away_score
    score = f"{hs}:{as_}"
    winner, loser = (h, a) if hs > as_ else (a, h)
    # Названия команд не склоняем: только именительный падеж и «Хозяева — Гости счёт».
    line = f"{h} — {a} {score}"
    if facts.get("сенсация") and hs != as_:
        title = rng.choice([f"😱 Сенсация: {line}", f"😱 Прогнозы не сбылись: {line}"])
    elif hs == as_:
        title = rng.choice([f"🤝 Ничья: {line}", f"🤝 Очки поровну: {line}"])
    elif facts.get("главный_момент", "").startswith("Гол на"):
        title = rng.choice([f"🔥 Победа в концовке: {line}", f"🔥 Всё решила концовка: {line}"])
    elif abs(hs - as_) >= 3:
        title = rng.choice([f"💥 Разгром: {line}", f"💥 {winner} не оставил шансов: {line}"])
    else:
        title = rng.choice([f"⚽ {winner} побеждает: {line}", f"⚽ Три очка — {winner}: {line}", f"⚽ {line}"])
    parts = [f"<b>{_esc(title)}</b>"]
    if facts.get("голы"):
        goals = ", ".join(f"{g['счёт_после'] or ''} — {g['автор'] or g['команда']} ({g['минута']}')".strip(" —")
                          for g in facts["голы"])
        parts.append(f"⚽ {_esc(goals)}")
    if facts.get("главный_момент"):
        parts.append(f"⏱ {_esc(facts['главный_момент'])}.")
    st = facts.get("статистика_хозяева_гости") or {}
    bits = []
    if "xG" in st:
        bits.append(f"xG {st['xG'][0]} – {st['xG'][1]}")
    if "владение_%" in st:
        bits.append(f"владение {st['владение_%'][0]}% – {st['владение_%'][1]}%")
    if "удары_в_створ" in st:
        bits.append(f"в створ {st['удары_в_створ'][0]} – {st['удары_в_створ'][1]}")
    if bits:
        parts.append("📊 " + ", ".join(bits) + ".")
    fans = []
    if facts.get("лучшие"):
        hero = facts["лучшие"][0]
        fans.append(f"герой — {_esc(hero['игрок'])}, {_score1(hero['оценка'])}")
    if facts.get("антигерой"):
        anti = facts["антигерой"]
        fans.append(f"антигерой — {_esc(anti['игрок'])}, {_score1(anti['оценка'])}")
    if fans:
        parts.append(f"⭐ <b>Болельщики ({facts['оценок_болельщиков']} оценок):</b> " + "; ".join(fans) + ".")
    if facts.get("раскол_трибун"):
        s = facts["раскол_трибун"]
        parts.append(f"⚖️ {_esc(s['игрок'])} расколол трибуны: свои фанаты — {s['свои_фанаты']}, чужие — {s['фанаты_соперника']}.")
    ref = facts.get("судья") or {}
    if ref.get("разрыв") and ref["разрыв"] >= 1.5:
        parts.append(f"🟨 Судья {_esc(ref['имя'])} разделил трибуны: болельщики хозяев поставили {ref['фанаты_хозяев']}, гостей — {ref['фанаты_гостей']}.")
    for team, pos in (facts.get("таблица") or {}).items():
        verb = "поднялся" if pos["стало_место"] < pos["было_место"] else "опустился"
        parts.append(f"📈 {_esc(team)} {verb} на {pos['стало_место']}-е место.")
    if facts.get("эксперт"):
        e = facts["эксперт"]
        parts.append(f"🎙 <i>«{_esc(e['цитата'])}»</i> — {_esc(e['имя'])}")
    return "\n\n".join(parts)


# ---------------- Превью тура
def preview_facts(tour, matches: list) -> tuple[dict, object]:
    """Факты превью и главный матч тура (для опроса)."""
    from engagement.models import ExpertTake
    from matches.card_services import _recent_meetings, _summarize_h2h
    from matches.services import describe_intrigue
    from predictions.services import prediction_counts
    from teams.models import TeamSeasonStats
    from teams.services import describe_season_form_streak

    from .channel import kickoff

    out = {"тур": tour, "матчи": []}
    main, main_score = None, -1
    for m in matches:
        pos = dict(TeamSeasonStats.objects.filter(season=m.season, team_id__in=[m.home_team_id, m.away_team_id])
                   .values_list("team_id", "position"))
        recent = _recent_meetings(m)
        intrigue = describe_intrigue(m, home_position=pos.get(m.home_team_id), away_position=pos.get(m.away_team_id),
                                     last_meeting=recent[0] if recent else None)
        row = {"хозяева": m.home_team.name, "гости": m.away_team.name, "когда": kickoff(m)}
        if pos.get(m.home_team_id) and pos.get(m.away_team_id):
            row["места_в_таблице"] = [pos[m.home_team_id], pos[m.away_team_id]]
        for side, team in (("форма_хозяев", m.home_team), ("форма_гостей", m.away_team)):
            played = list(type(m).objects.filter(Q(home_team=team) | Q(away_team=team), season=m.season, status="finished",
                                                 start_time__lt=m.start_time).select_related("home_team", "away_team")
                          .order_by("-start_time"))
            text = describe_season_form_streak(team, played)
            if text:
                row[side] = text
        h2h = _summarize_h2h(m, recent)
        if h2h:
            row["очные_последние"] = {f"победы_{m.home_team.name}": h2h["home_wins"], "ничьи": h2h["draws"],
                                      f"победы_{m.away_team.name}": h2h["away_wins"]}
        counts = prediction_counts(m)
        if counts["total"] >= 5:
            row["прогнозы_%"] = {m.home_team.name: counts["home_pct"], "ничья": counts["draw_pct"], m.away_team.name: counts["away_pct"]}
        if intrigue:
            row["интрига"] = intrigue
        take = ExpertTake.objects.filter(match=m, is_published=True).select_related("expert").first()
        if take:
            row["эксперт"] = {"имя": take.display_name, "цитата": take.headline or take.text[:200]}
        score = (3 if intrigue else 0) + counts["total"] / 100
        if score > main_score:
            main, main_score = m, score
        out["матчи"].append(row)
    return out, main


def preview_template(facts: dict) -> str:
    rng = _rng(f"preview:{facts['тур']}:{len(facts['матчи'])}")
    head = f"{facts['тур']}-й тур КПЛ" if facts["тур"] else "Ближайшие матчи КПЛ"
    title = rng.choice([f"🗓 {head}: что на кону", f"🗓 {head} — главное перед игрой", f"🗓 Впереди {head}"])
    parts = [f"<b>{_esc(title)}</b>"]
    lines = []
    for r in facts["матчи"]:
        line = f"• <b>{_esc(r['хозяева'])} – {_esc(r['гости'])}</b>, {_esc(r['когда'])}"
        extra = []
        if r.get("интрига"):
            extra.append(r["интрига"])
        if r.get("места_в_таблице"):
            extra.append(f"{r['места_в_таблице'][0]}-е против {r['места_в_таблице'][1]}-го")
        if r.get("прогнозы_%"):
            fav = max(r["прогнозы_%"], key=r["прогнозы_%"].get)
            if fav != "ничья":
                extra.append(f"прогнозы: {fav} — {r['прогнозы_%'][fav]}%")
        if extra:
            line += "\n   " + _esc(" · ".join(extra))
        lines.append(line)
    parts.append("\n".join(lines))
    quotes = [r for r in facts["матчи"] if r.get("эксперт")]
    if quotes:
        q = quotes[0]
        parts.append(f"🎙 <i>«{_esc(q['эксперт']['цитата'])}»</i> — {_esc(q['эксперт']['имя'])}")
    parts.append("🔮 Сделайте прогноз до стартового свистка, а после матча оцените игроков.")
    return "\n\n".join(parts)


# ---------------- Итоги тура
def round_facts(rnd) -> dict:
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display
    from engagement.social import controversial_referee
    from evaluations.models import EvaluationSession
    from matches.models import Match
    from matches.services import compute_sensation_index
    from predictions.services import prediction_counts

    matches = list(Match.objects.filter(season=rnd.season, tour=rnd.tour, status="finished").select_related("home_team", "away_team"))
    facts: dict = {"тур": rnd.tour, "матчей": len(matches),
                   "оценок_болельщиков": EvaluationSession.objects.filter(match__in=matches, status="completed").count()}
    if rnd.player_of_round_name:
        facts["игрок_тура"] = {"имя": rnd.player_of_round_name, "команда": rnd.player_of_round_team_name,
                               "оценка": _r(rnd.player_of_round_score), "голосов": rnd.player_of_round_votes}
    squad = [s.occupant_name for s in rnd.slots.order_by("order") if s.occupant_name]
    if squad:
        facts["сборная_тура"] = squad
    if rnd.most_dramatic_match_id:
        m = rnd.most_dramatic_match
        facts["самый_драматичный"] = {"матч": f"{m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}",
                                      "индекс_драмы_из_100": _r(rnd.most_dramatic_match_score)}
    ref, gap = controversial_referee(rnd.season, rnd.tour)
    if ref and gap is not None:
        facts["спорный_судья"] = {"имя": ref.referee.full_name, "матч": f"{ref.match.home_team.name} – {ref.match.away_team.name}",
                                  "фанаты_хозяев": _r(ref.home_fans_avg), "фанаты_гостей": _r(ref.away_fans_avg), "разрыв": _r(gap)}
    best_upset = None
    for m in matches:
        idx = compute_sensation_index(m, prediction_counts(m))
        if idx and (not best_upset or idx > best_upset[0]):
            best_upset = (idx, m)
    if best_upset:
        m = best_upset[1]
        facts["сенсация_тура"] = {"матч": f"{m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}",
                                  "ждали_другого_%": best_upset[0]}
    top = (PlayerMatchAggregate.objects.filter(match__in=matches, total_votes__gte=min_votes_for_display())
           .select_related("player__team").order_by("-performance_score"))
    seen, leaders = set(), []
    for a in top:
        if a.player_id not in seen:
            seen.add(a.player_id)
            leaders.append({"игрок": a.player.full_name, "команда": a.player.team.name if a.player.team_id else "",
                            "оценка": _r(a.performance_score)})
        if len(leaders) == 4:
            break
    if leaders:
        facts["лидеры_оценок"] = leaders
    return facts


def round_has_story(facts: dict) -> bool:
    return any(facts.get(k) for k in ("игрок_тура", "лидеры_оценок", "спорный_судья", "сенсация_тура"))


def round_template(facts: dict) -> str:
    rng = _rng(f"round:{facts['тур']}")
    title = rng.choice([f"🏆 Итоги {facts['тур']}-го тура глазами трибун", f"🏆 {facts['тур']}-й тур: главное",
                        f"🏆 Как болельщики оценили {facts['тур']}-й тур"])
    parts = [f"<b>{_esc(title)}</b>"]
    if facts["оценок_болельщиков"]:
        parts.append(f"📊 {facts['матчей']} матчей, {facts['оценок_болельщиков']} оценок болельщиков.")
    if facts.get("игрок_тура"):
        p = facts["игрок_тура"]
        parts.append(f"⭐ <b>Игрок тура</b> — {_esc(p['имя'])}" + (f" ({_esc(p['команда'])})" if p["команда"] else "")
                     + (f": {p['оценка']} из 10" if p["оценка"] is not None else "") + ".")
    elif facts.get("лидеры_оценок"):
        leaders = facts["лидеры_оценок"][:3]
        parts.append("⭐ <b>Лучшие оценки тура:</b> " + _esc(", ".join(f"{l['игрок']} {_score1(l['оценка'])}" for l in leaders)) + ".")
    if facts.get("сборная_тура"):
        parts.append("🧩 <b>Сборная тура:</b> " + _esc(", ".join(facts["сборная_тура"])) + ".")
    if facts.get("сенсация_тура"):
        s = facts["сенсация_тура"]
        parts.append(f"😱 <b>Сенсация:</b> {_esc(s['матч'])} — {s['ждали_другого_%']}% болельщиков ждали другого.")
    if facts.get("самый_драматичный"):
        d = facts["самый_драматичный"]
        parts.append(f"🔥 <b>Драма тура:</b> {_esc(d['матч'])}" + (f", индекс драмы {d['индекс_драмы_из_100']} из 100" if d["индекс_драмы_из_100"] else "") + ".")
    if facts.get("спорный_судья"):
        r = facts["спорный_судья"]
        parts.append(f"🟨 <b>Спорный судья:</b> {_esc(r['имя'])} ({_esc(r['матч'])}) — фанаты поставили {r['фанаты_хозяев']} и {r['фанаты_гостей']}.")
    return "\n\n".join(parts)


def round_poll(facts: dict) -> dict | None:
    names = []
    for p in ([facts["игрок_тура"]["имя"]] if facts.get("игрок_тура") else []) + [l["игрок"] for l in facts.get("лидеры_оценок", [])]:
        if p not in names:
            names.append(p)
    if len(names) < 2:
        return None
    return {"question": f"Кто, по-вашему, лучший игрок {facts['тур']}-го тура?", "options": names[:4]}


# ---------------- Рубрики
def controversy_facts(rnd) -> dict | None:
    from engagement.social import controversial_referee

    ref, gap = controversial_referee(rnd.season, rnd.tour)
    if not ref or gap is None or gap < 1:
        return None
    m = ref.match
    return {"тур": rnd.tour, "судья": ref.referee.full_name, "матч": f"{m.home_team.name} {m.home_score}:{m.away_score} {m.away_team.name}",
            "хозяева": m.home_team.name, "гости": m.away_team.name, "фанаты_хозяев": _r(ref.home_fans_avg),
            "фанаты_гостей": _r(ref.away_fans_avg), "разрыв": _r(gap), "общая_оценка": _r(ref.performance_score)}


def controversy_template(f: dict) -> str:
    return "\n\n".join([
        f"<b>🟨 Спорный момент {f['тур']}-го тура: судья {_esc(f['судья'])}</b>",
        f"Матч {_esc(f['матч'])} развёл трибуны по разные стороны: болельщики хозяев ({_esc(f['хозяева'])}) оценили "
        f"судейство на {f['фанаты_хозяев']}, гостей ({_esc(f['гости'])}) — на {f['фанаты_гостей']}. Разрыв — {f['разрыв']}.",
        "А как считаете вы? Голосуйте ниже 👇",
    ])


def controversy_poll(f: dict) -> dict:
    return {"question": f"Как отсудил {f['судья']} матч {f['хозяева']} – {f['гости']}?",
            "options": ["Справедливо", f"Ошибался в пользу хозяев ({f['хозяева']})", f"Ошибался в пользу гостей ({f['гости']})"]}


def number_facts(rnd) -> dict | None:
    """«Цифра недели»: самая яркая цифра последнего тура, тема меняется по неделям."""
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display
    from evaluations.models import EvaluationSession
    from matches.models import Match

    matches = list(Match.objects.filter(season=rnd.season, tour=rnd.tour, status="finished"))
    aggs = list(PlayerMatchAggregate.objects.filter(match__in=matches, total_votes__gte=min_votes_for_display())
                .select_related("player__team", "match__home_team", "match__away_team"))
    options = []
    split = max((a for a in aggs if a.own_fans_avg is not None and a.rival_fans_avg is not None),
                key=lambda a: abs(a.own_fans_avg - a.rival_fans_avg), default=None)
    if split and abs(split.own_fans_avg - split.rival_fans_avg) >= 2:
        options.append({"тема": "раскол трибун", "цифра": _r(abs(split.own_fans_avg - split.rival_fans_avg)),
                        "игрок": split.player.full_name, "свои_фанаты": _r(split.own_fans_avg),
                        "фанаты_соперника": _r(split.rival_fans_avg),
                        "матч": f"{split.match.home_team.name} – {split.match.away_team.name}"})
    if aggs:
        best = max(aggs, key=lambda a: a.performance_score)
        options.append({"тема": "лучшая оценка тура", "цифра": _r(best.performance_score), "игрок": best.player.full_name,
                        "матч": f"{best.match.home_team.name} – {best.match.away_team.name}", "голосов": best.total_votes})
    votes = EvaluationSession.objects.filter(match__in=matches, status="completed").count()
    if votes:
        options.append({"тема": "оценок за тур", "цифра": votes, "матчей": len(matches)})
    if not options:
        return None
    pick = options[timezone.localdate().isocalendar()[1] % len(options)]
    return {"тур": rnd.tour, **pick}


def number_template(f: dict) -> str:
    if f["тема"] == "раскол трибун":
        body = (f"На столько разошлись трибуны в оценке одного игрока. {_esc(f['игрок'])}, матч {_esc(f['матч'])}: "
                f"свои болельщики поставили {f['свои_фанаты']}, соперники — {f['фанаты_соперника']}.")
    elif f["тема"] == "лучшая оценка тура":
        body = f"Лучшая оценка {f['тур']}-го тура. {_esc(f['игрок'])}, матч {_esc(f['матч'])} — {f['голосов']} голосов болельщиков."
    else:
        body = f"Столько оценок болельщики поставили игрокам, тренерам и судьям за {f['матчей']} матчей {f['тур']}-го тура."
    return f"<b>🔢 Цифра недели: {f['цифра']}</b>\n\n{body}\n\nА вы оценили свой матч?"


def latest_final_round():
    from round_squad.models import RoundBestXI

    since = timezone.now() - timedelta(days=10)
    return (RoundBestXI.objects.filter(is_final=True, finalized_at__gte=since).select_related("season", "most_dramatic_match__home_team",
                                                                                          "most_dramatic_match__away_team")
            .order_by("-finalized_at").first())
