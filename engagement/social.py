# engagement/social.py
"""Еженедельный контент для соцсетей: картинки и подписи по итогам последнего тура.
«Игрок тура по мнению болельщиков», «Самый спорный судья», «Самый драматичный матч»,
карточка «Лучшие тура». Спорность судьи — разрыв оценок фанатов двух команд."""
from __future__ import annotations

from django.core.cache import cache
from django.core.files.storage import default_storage
from core.utils import ru_num

CACHE_TTL = 60 * 60
HASHTAGS = "#DOPX #КПЛ #футболКазахстана"


def _latest_round():
    from round_squad.models import RoundBestXI

    return (
        RoundBestXI.objects.select_related("season", "most_dramatic_match__home_team", "most_dramatic_match__away_team")
        .order_by("-is_final", "-season__year", "-tour").first()
    )


def controversial_referee(season, tour):
    """(агрегат, разрыв) — судья тура, по которому сильнее всего спорили фанаты."""
    from aggregates.models import RefereeMatchAggregate
    from aggregates.services import min_votes_for_display

    aggs = list(
        RefereeMatchAggregate.objects.filter(
            match__season=season, match__tour=tour, total_votes__gte=min_votes_for_display(),
        ).select_related("referee", "match__home_team", "match__away_team")
    )
    rated = [a for a in aggs if a.home_fans_avg is not None and a.away_fans_avg is not None]
    if rated:
        top = max(rated, key=lambda a: abs(a.home_fans_avg - a.away_fans_avg))
        return top, abs(top.home_fans_avg - top.away_fans_avg)
    if aggs:
        return min(aggs, key=lambda a: a.performance_score), None
    return None, None


def weekly_content(force: bool = False) -> dict | None:
    """Готовые посты: картинка (URL), заголовок, подпись. Кэш на час."""
    from core.services.share_cards import build_social_card

    rnd = _latest_round()
    if rnd is None:
        return None
    key = f"social:weekly:{rnd.pk}:{rnd.last_computed_at and rnd.last_computed_at.timestamp()}"
    if not force and (data := cache.get(key)):
        return data

    posts = []
    title = f"{rnd.tour}-й тур · сезон {rnd.season.year}"
    if rnd.player_of_round_name and rnd.player_of_round_score is not None:
        path = build_social_card(
            kind="player_of_round", eyebrow="ИГРОК ТУРА ПО МНЕНИЮ БОЛЕЛЬЩИКОВ",
            number_text=f"{ru_num(rnd.player_of_round_score, 1)}", label_line1=rnd.player_of_round_name,
            label_line2=f"{rnd.player_of_round_team_name} · {rnd.tour}-й тур".strip(" ·"),
            image_url=rnd.player_of_round_photo_url,
        )
        posts.append({
            "key": "player_of_round", "icon": "ti-star", "title": "Игрок тура по мнению болельщиков",
            "image": default_storage.url(path),
            "caption": (
                f"⭐ Игрок {rnd.tour}-го тура по мнению болельщиков: {rnd.player_of_round_name}"
                f"{f' ({rnd.player_of_round_team_name})' if rnd.player_of_round_team_name else ''}: "
                f"{ru_num(rnd.player_of_round_score, 1)} из 10 по {rnd.player_of_round_votes} голосам.\n"
                f"Согласны? Оцените матчи тура на DOPX.\n{HASHTAGS}"
            ),
        })

    ref_agg, gap = controversial_referee(rnd.season, rnd.tour)
    if ref_agg:
        match = ref_agg.match
        match_label = f"{match.home_team.name} — {match.away_team.name}"
        if gap is not None:
            number, line2 = f"{ru_num(gap, 1)}", f"разрыв оценок фанатов · {match_label}"
            detail = (f"фанаты {match.home_team.name} поставили {ru_num(ref_agg.home_fans_avg, 1)}, "
                      f"фанаты {match.away_team.name} {ru_num(ref_agg.away_fans_avg, 1)}")
        else:
            number, line2 = f"{ru_num(ref_agg.performance_score, 1)}", f"оценка болельщиков · {match_label}"
            detail = f"средняя оценка судейства {ru_num(ref_agg.performance_score, 1)} из 10"
        path = build_social_card(kind="referee", eyebrow="САМЫЙ СПОРНЫЙ СУДЬЯ ТУРА", number_text=number,
                                 label_line1=ref_agg.referee.full_name, label_line2=line2)
        posts.append({
            "key": "referee", "icon": "ti-gavel", "title": "Самый спорный судья",
            "image": default_storage.url(path),
            "caption": (
                f"🟨 Самый спорный судья {rnd.tour}-го тура: {ref_agg.referee.full_name}, {match_label}. "
                f"{detail[0].upper()}{detail[1:]}.\nА вы как оценили? DOPX.\n{HASHTAGS}"
            ),
        })

    if rnd.most_dramatic_match_id and rnd.most_dramatic_match_score is not None:
        m = rnd.most_dramatic_match
        path = build_social_card(
            kind="drama", eyebrow="САМЫЙ ДРАМАТИЧНЫЙ МАТЧ ТУРА", number_text=f"{ru_num(rnd.most_dramatic_match_score, 1)}",
            label_line1=f"{m.home_team.name} {m.get_score_display()} {m.away_team.name}", label_line2="индекс драмы по оценкам болельщиков",
        )
        posts.append({
            "key": "drama", "icon": "ti-flame", "title": "Самый драматичный матч",
            "image": default_storage.url(path),
            "caption": (
                f"🔥 Самый драматичный матч {rnd.tour}-го тура: {m.home_team.name} {m.get_score_display()} "
                f"{m.away_team.name}. Индекс драмы {ru_num(rnd.most_dramatic_match_score, 1)}.\n{HASHTAGS}"
            ),
        })

    card = rnd.current_share_card()
    if card and default_storage.exists(card):
        posts.append({
            "key": "round_squad", "icon": "ti-shirt", "title": rnd.brand_title,
            "image": default_storage.url(card),
            "caption": f"🏆 {rnd.brand_title}: сборная тура по оценкам болельщиков.\n{HASHTAGS}",
        })

    data = {"title": title, "is_final": rnd.is_final, "posts": posts}
    cache.set(key, data, CACHE_TTL)
    return data
