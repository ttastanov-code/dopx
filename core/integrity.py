# core/integrity.py
"""Проверка целостности данных: правила «это всегда должно сходиться» по всей базе.
Каждая проверка возвращает число нарушений и примеры; запускается ежедневно и из «Здоровья данных».
Только чтение — ничего не исправляет, исправление — ресинк или пересчёт по найденному."""
from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Count, Exists, F, OuterRef, Q

GOAL_TYPES = ("goal", "penalty", "own_goal")
SAMPLES = 10


@dataclass
class Finding:
    code: str
    title: str
    severity: str  # error — неверные цифры на сайте; warning — подозрительно, надо глянуть
    count: int
    samples: list[str] = field(default_factory=list)
    hint: str = ""


def _match_label(m) -> str:
    return f"{m.home_team} — {m.away_team} ({m.start_time:%d.%m.%Y}, id {m.id})"


# ---------------------------------------------------------------------------
# Матчи и события
# ---------------------------------------------------------------------------

def score_vs_events_qs():
    """Завершённые матчи, где голы в ленте не сходятся со счётом; пустая лента при счёте > 0 — тоже."""
    from matches.models import Match

    goals = Q(events__event_type__in=GOAL_TYPES)
    return (
        Match.objects.filter(status="finished", decided_administratively=False,
                             home_score__isnull=False, away_score__isnull=False)
        .annotate(ev_home=Count("events", filter=goals & Q(events__team_side="home"), distinct=True),
                  ev_away=Count("events", filter=goals & Q(events__team_side="away"), distinct=True))
        .exclude(ev_home=F("home_score"), ev_away=F("away_score"))
    )


def check_score_vs_events() -> Finding:
    qs = score_vs_events_qs().select_related("home_team", "away_team").order_by("-start_time")
    return Finding(
        "score_vs_events", "Голы в ленте не сходятся со счётом", "error", qs.count(),
        [f"{_match_label(m)}: счёт {m.home_score}:{m.away_score}, в событиях {m.ev_home}:{m.ev_away}" for m in qs[:SAMPLES]],
        "Ресинк матча в «Здоровье данных». Повторяется — смотреть импорт событий.",
    )


def check_finished_without_score() -> Finding:
    from matches.models import Match

    qs = Match.objects.filter(status="finished", decided_administratively=False).filter(
        Q(home_score__isnull=True) | Q(away_score__isnull=True)).select_related("home_team", "away_team")
    return Finding("finished_without_score", "Завершённый матч без счёта", "error", qs.count(),
                   [_match_label(m) for m in qs[:SAMPLES]], "Не попадает в таблицу. Ресинк матча.")


def check_event_player_side() -> Finding:
    """Автор гола или карточки в заявке другой команды (own_goal не в счёт: там игрок соперника)."""
    from events.models import MatchEvent
    from lineups.models import MatchLineupPlayer

    in_other_lineup = MatchLineupPlayer.objects.filter(
        lineup__match_id=OuterRef("match_id"), player_id=OuterRef("player_id"),
    ).exclude(lineup__side=OuterRef("team_side"))
    qs = (MatchEvent.objects.filter(player__isnull=False, event_type__in=("goal", "penalty", "yellow_card", "red_card"))
          .filter(Exists(in_other_lineup)).select_related("match__home_team", "match__away_team", "player"))
    return Finding(
        "event_player_side", "Автор события играет за другую команду", "error", qs.count(),
        [f"{_match_label(e.match)}: {e.get_event_type_display()} {e.display_minute}' — {e.player}" for e in qs[:SAMPLES]],
        "Неверная сторона события или игрок склеен не с тем человеком.",
    )


def check_event_player_not_in_lineup() -> Finding:
    """Автор гола не числится в заявке матча, хотя заявка загружена."""
    from events.models import MatchEvent
    from lineups.models import MatchLineup, MatchLineupPlayer

    in_lineup = MatchLineupPlayer.objects.filter(lineup__match_id=OuterRef("match_id"), player_id=OuterRef("player_id"))
    has_lineups = MatchLineup.objects.filter(match_id=OuterRef("match_id"))
    qs = (MatchEvent.objects.filter(player__isnull=False, event_type__in=GOAL_TYPES)
          .filter(Exists(has_lineups)).exclude(Exists(in_lineup))
          .select_related("match__home_team", "match__away_team", "player"))
    return Finding(
        "event_player_not_in_lineup", "Автор гола не в заявке матча", "warning", qs.count(),
        [f"{_match_label(e.match)}: {e.display_minute}' — {e.player}" for e in qs[:SAMPLES]],
        "Дубль игрока (разные записи одного человека) или пробел в составе у поставщика.",
    )


def check_event_minutes() -> Finding:
    from events.models import MatchEvent

    qs = MatchEvent.objects.filter(Q(minute__gt=130) | Q(added_time__gt=30)).select_related(
        "match__home_team", "match__away_team")
    return Finding("event_minutes", "Событие с невозможной минутой", "warning", qs.count(),
                   [f"{_match_label(e.match)}: {e.display_minute}'" for e in qs[:SAMPLES]])


def check_lineup_flag() -> Finding:
    from matches.models import Match

    with_flag = Match.objects.filter(status="finished", has_lineup=True, lineups__isnull=True)
    without_flag = Match.objects.filter(has_lineup=False, lineups__isnull=False).distinct()
    rows = list(with_flag.select_related("home_team", "away_team")[:SAMPLES])
    return Finding(
        "lineup_flag", "Флаг «есть состав» не совпадает с данными", "warning",
        with_flag.count() + without_flag.count(),
        [f"{_match_label(m)}: флаг есть, составов нет" for m in rows],
        "Влияет на оценку игроков: вайзард берёт состав по флагу.",
    )


def check_lineup_starters() -> Finding:
    """В стартовом составе завершённого матча не 11 человек."""
    from lineups.models import MatchLineup

    qs = (MatchLineup.objects.filter(match__status="finished")
          .annotate(starters=Count("players", filter=Q(players__is_starting=True)))
          .exclude(starters=11).select_related("match__home_team", "match__away_team", "team"))
    return Finding("lineup_starters", "В старте не 11 игроков", "warning", qs.count(),
                   [f"{_match_label(l.match)}: {l.team} — {l.starters} в старте" for l in qs[:SAMPLES]])


# ---------------------------------------------------------------------------
# Турнирная таблица
# ---------------------------------------------------------------------------

def check_standings() -> Finding:
    """Таблица активных сезонов пересчитывается из результатов и сверяется с сохранённой."""
    from matches.models import Match
    from seasons.models import Season
    from teams.models import TeamSeasonStats

    samples, bad = [], 0
    for season in Season.objects.filter(is_active=True):
        calc: dict = {}
        for m in Match.objects.filter(season=season, status="finished", home_score__isnull=False,
                                      away_score__isnull=False).values("home_team_id", "away_team_id",
                                                                       "home_score", "away_score"):
            for team_id, gf, ga in ((m["home_team_id"], m["home_score"], m["away_score"]),
                                    (m["away_team_id"], m["away_score"], m["home_score"])):
                s = calc.setdefault(team_id, {"played": 0, "wins": 0, "draws": 0, "losses": 0,
                                              "goals_scored": 0, "goals_conceded": 0})
                s["played"] += 1
                s["goals_scored"] += gf
                s["goals_conceded"] += ga
                s["wins" if gf > ga else "draws" if gf == ga else "losses"] += 1
        for row in TeamSeasonStats.objects.filter(season=season).select_related("team"):
            s = calc.get(row.team_id, {"played": 0, "wins": 0, "draws": 0, "losses": 0,
                                       "goals_scored": 0, "goals_conceded": 0})
            s = {**s, "points": s["wins"] * 3 + s["draws"], "goal_diff": s["goals_scored"] - s["goals_conceded"]}
            diff = [f"{k} {getattr(row, k)}≠{v}" for k, v in s.items() if getattr(row, k) != v]
            if diff:
                bad += 1
                samples.append(f"{season}: {row.team} — " + ", ".join(diff))
    return Finding("standings", "Турнирная таблица не сходится с результатами", "error", bad, samples[:SAMPLES],
                   "Пересчитать таблицу (recalculate_season_standings). Повторяется — смотреть, кто пишет в таблицу.")


# ---------------------------------------------------------------------------
# Рейтинги
# ---------------------------------------------------------------------------

def _countable_counts(model, entity_field: str, match_ids) -> dict:
    """{(match_id, entity_id): засчитанных голосов} — те же правила, что у пересчёта агрегатов."""
    from aggregates.services import countable_evaluations

    result = {}
    for match_id in match_ids:
        # У оценки судьи нет поля судьи — он берётся из матча.
        field = "match__referee_id" if entity_field == "referee_id" else entity_field
        qs = model.objects.filter(match_id=match_id)
        if entity_field == "player_id":
            from lineups.models import MatchLineupPlayer

            # Голоса за невышедших запасных в рейтинг не идут (aggregates.tasks).
            qs = qs.exclude(player_id__in=MatchLineupPlayer.objects.filter(lineup__match_id=match_id).unused()
                            .values("player_id"))
        rows = countable_evaluations(qs, match_id).values(field).annotate(n=Count("id"))
        for row in rows:
            result[(match_id, row[field])] = row["n"]
    return result


def check_aggregate_votes() -> Finding:
    """total_votes в рейтингах игроков, команд, тренеров и судей = засчитанные голоса."""
    from aggregates.models import CoachMatchAggregate, PlayerMatchAggregate, RefereeMatchAggregate, TeamMatchAggregate
    from evaluations.models import CoachEvaluation, PlayerEvaluation, RefereeEvaluation, TeamEvaluation

    pairs = (
        ("игрок", PlayerMatchAggregate, PlayerEvaluation, "player_id"),
        ("команда", TeamMatchAggregate, TeamEvaluation, "team_id"),
        ("тренер", CoachMatchAggregate, CoachEvaluation, "coach_id"),
        ("судья", RefereeMatchAggregate, RefereeEvaluation, "referee_id"),
    )
    samples, bad = [], 0
    for label, agg_model, eval_model, entity in pairs:
        match_ids = set(agg_model.objects.values_list("match_id", flat=True).distinct()) | set(
            eval_model.objects.values_list("match_id", flat=True).distinct())
        expected = _countable_counts(eval_model, entity, match_ids)
        stored = {(r["match_id"], r[entity]): r["total_votes"]
                  for r in agg_model.objects.values("match_id", entity, "total_votes")}
        for key in set(expected) | set(stored):
            want, have = expected.get(key, 0), stored.get(key)
            if have is None and want == 0:
                continue
            if have != want:
                bad += 1
                if len(samples) < SAMPLES:
                    samples.append(f"{label} {key[1]} в матче {key[0]}: в рейтинге {have if have is not None else 'нет'}, "
                                   f"засчитано {want}")
    return Finding("aggregate_votes", "Число голосов в рейтинге не равно засчитанным", "error", bad, samples,
                   "Пересчитать агрегаты матча. Повторяется — пересчёт не запускается после голосования или удаления.")


def check_match_aggregate_votes() -> Finding:
    from aggregates.models import MatchAggregate
    from evaluations.models import MatchEvaluation

    match_ids = set(MatchAggregate.objects.values_list("match_id", flat=True)) | set(
        MatchEvaluation.objects.values_list("match_id", flat=True).distinct())
    from aggregates.services import countable_evaluations

    samples, bad = [], 0
    stored = dict(MatchAggregate.objects.values_list("match_id", "total_votes"))
    for match_id in match_ids:
        want = countable_evaluations(MatchEvaluation.objects.filter(match_id=match_id), match_id).count()
        have = stored.get(match_id)
        if (have or 0) != want:
            bad += 1
            if len(samples) < SAMPLES:
                samples.append(f"матч {match_id}: в агрегате {have}, засчитано {want}")
    return Finding("match_aggregate_votes", "Голоса за матч в агрегате не равны засчитанным", "error", bad, samples)


def check_score_ranges() -> Finding:
    """Рейтинги в своих шкалах: 1–10 у игроков и команд."""
    from aggregates.models import PlayerMatchAggregate, TeamMatchAggregate

    bad_q = Q(performance_score__lt=1) | Q(performance_score__gt=10)
    qs_p = PlayerMatchAggregate.objects.filter(total_votes__gt=0).filter(bad_q)
    qs_t = TeamMatchAggregate.objects.filter(total_votes__gt=0).filter(bad_q)
    samples = [f"игрок {a.player_id} матч {a.match_id}: {a.performance_score}" for a in qs_p[:SAMPLES]]
    return Finding("score_ranges", "Рейтинг вне шкалы 1–10", "error", qs_p.count() + qs_t.count(), samples)


def check_votes_outside_window() -> Finding:
    """Голос за матч, который ещё не закончился, или после закрытия голосования."""
    from evaluations.models import EvaluationSession

    qs = EvaluationSession.objects.filter(status="completed").filter(
        Q(match__status__in=("scheduled", "live")) | Q(completed_at__gt=F("match__voting_open_until")))
    samples = [f"сессия {s.id}: матч {s.match_id}, завершена {s.completed_at:%d.%m %H:%M}" for s in qs[:SAMPLES]]
    return Finding("votes_outside_window", "Оценка вне окна голосования", "warning", qs.count(), samples,
                   "Проверка политики голосования пропустила оценку.")


def check_votes_for_players_outside_lineup() -> Finding:
    from evaluations.models import PlayerEvaluation
    from lineups.models import MatchLineupPlayer

    in_lineup = MatchLineupPlayer.objects.filter(lineup__match_id=OuterRef("match_id"), player_id=OuterRef("player_id"))
    qs = PlayerEvaluation.objects.exclude(Exists(in_lineup))
    return Finding("votes_outside_lineup", "Оценка игрока, которого нет в заявке матча", "warning", qs.count(),
                   [f"игрок {e.player_id} матч {e.match_id}" for e in qs[:SAMPLES]])


def check_rating_for_unused_substitute() -> Finding:
    """Рейтинг за матч у игрока, который не выходил на поле (запасной без замены)."""
    from aggregates.models import PlayerMatchAggregate
    from lineups.models import MatchLineupPlayer

    unused = MatchLineupPlayer.objects.filter(lineup__match_id=OuterRef("match_id"),
                                              player_id=OuterRef("player_id")).unused()
    qs = PlayerMatchAggregate.objects.filter(Exists(unused)).select_related("player", "match__home_team", "match__away_team")
    return Finding("rating_unused_sub", "Рейтинг у игрока, который не выходил на поле", "error", qs.count(),
                   [f"{a.player} — {_match_label(a.match)}" for a in qs[:SAMPLES]],
                   "Пересчитать агрегаты матча: голоса за невышедших в рейтинг не идут.")


# ---------------------------------------------------------------------------
# Пользователи и прогресс
# ---------------------------------------------------------------------------

def check_user_evaluation_counter() -> Finding:
    from users.models import User

    qs = (User.objects.annotate(done=Count("evaluation_sessions", filter=Q(evaluation_sessions__status="completed")))
          .exclude(total_evaluations=F("done")))
    return Finding("user_evaluation_counter", "Счётчик оценок пользователя не равен завершённым оценкам", "error",
                   qs.count(), [f"{u.username}: {u.total_evaluations}≠{u.done}" for u in qs[:SAMPLES]],
                   "Скрипт «Пересчитать XP, серии и достижения».")


def check_user_levels() -> Finding:
    from users.models import UserXP, level_for_total_xp

    bad = [xp for xp in UserXP.objects.select_related("user").only("level", "total_xp", "user__username")
           if xp.level != level_for_total_xp(xp.total_xp)]
    return Finding("user_levels", "Уровень не соответствует опыту", "error", len(bad),
                   [f"{x.user.username}: уровень {x.level}, по опыту {level_for_total_xp(x.total_xp)}" for x in bad[:SAMPLES]])


def check_prediction_streaks() -> Finding:
    """Серия угаданных прогнозов не может быть больше числа угаданных."""
    from predictions.models import MatchPrediction
    from users.models import User

    candidates = list(User.objects.filter(prediction_streak__gt=0).only("username", "prediction_streak"))
    bad = []
    for user in candidates:
        correct = sum(1 for p in MatchPrediction.objects.filter(user=user, match__status="finished")
                      .select_related("match") if p.is_correct)
        if user.prediction_streak > correct:
            bad.append(f"{user.username}: серия {user.prediction_streak}, угадано всего {correct}")
    return Finding("prediction_streaks", "Серия прогнозов больше числа угаданных", "error", len(bad), bad[:SAMPLES],
                   "Скрипт «Пересчитать XP, серии и достижения».")


# ---------------------------------------------------------------------------
# Сборные
# ---------------------------------------------------------------------------

def check_round_squads() -> Finding:
    """Зафиксированный тур с неполной сборной, хотя за его матчи есть рейтинги игроков."""
    from aggregates.models import PlayerMatchAggregate
    from aggregates.services import min_votes_for_display
    from round_squad.models import RoundBestXI

    rated = PlayerMatchAggregate.objects.filter(match__season_id=OuterRef("season_id"), match__tour=OuterRef("tour"),
                                                total_votes__gte=min_votes_for_display())
    qs = (RoundBestXI.objects.filter(is_final=True).filter(Exists(rated))
          # 11 игроков; слот тренера (COACH) не считаем.
          .annotate(filled=Count("slots", filter=Q(slots__object_id__isnull=False) & ~Q(slots__slot_code="COACH")))
          .exclude(filled=11))
    return Finding("round_squads", "Сборная тура неполная, хотя рейтинги есть", "error", qs.count(),
                   [f"{r.season} тур {r.tour}: {r.filled} из 11" for r in qs[:SAMPLES]],
                   "Скрипт «Пересчитать закрытые туры».")


CHECKS = (
    check_score_vs_events, check_finished_without_score, check_event_player_side, check_event_player_not_in_lineup,
    check_event_minutes, check_lineup_flag, check_lineup_starters, check_standings, check_aggregate_votes,
    check_match_aggregate_votes, check_score_ranges, check_votes_outside_window,
    check_votes_for_players_outside_lineup, check_rating_for_unused_substitute, check_user_evaluation_counter, check_user_levels,
    check_prediction_streaks, check_round_squads,
)


def run_all() -> list[Finding]:
    """Все проверки; упавшая проверка — отдельная находка, остальные идут дальше."""
    import logging

    results = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception("integrity %s упала", check.__name__)
            results.append(Finding(check.__name__, f"Проверка {check.__name__} упала", "error", 1, [str(exc)]))
    return results


CACHE_KEY = "integrity:last"
CACHE_TTL = 3 * 24 * 3600


def run_and_store() -> dict:
    """Прогон всех проверок; итог в кэше — его читают дашборд и алерты бота."""
    from dataclasses import asdict

    from django.core.cache import cache
    from django.utils import timezone

    findings = run_all()
    report = {"checked_at": timezone.now().isoformat(), "findings": [asdict(f) for f in findings],
              "errors": sum(f.count for f in findings if f.severity == "error"),
              "warnings": sum(f.count for f in findings if f.severity == "warning")}
    cache.set(CACHE_KEY, report, CACHE_TTL)
    return report


def last_report() -> dict | None:
    from django.core.cache import cache

    return cache.get(CACHE_KEY)
