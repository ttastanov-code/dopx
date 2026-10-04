# core/services/share_cards.py
"""Карточки для шеринга и соцсетей: прежние функции и параметры, рисует core.cards (SVG-шаблоны templates/cards/).
Логотипы клубов и фото игроков — необязательные ссылки; без них карточка рисуется с инициалами или знаком DOPX."""
from __future__ import annotations

import hashlib

from core import cards

CARD_SIZE = cards.OG


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _rating(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"


def build_match_share_card(*, home_team: str, away_team: str, home_score: int, away_score: int,
                           top_player_name: str, top_player_score: float, home_logo: str = "", away_logo: str = "",
                           top_player_photo: str = "") -> str:
    hero = top_player_name if top_player_name and top_player_name != "—" else ""
    return cards.match_card(home=home_team, away=away_team, score=f"{home_score} : {away_score}",
                            home_logo=home_logo, away_logo=away_logo, hero_name=hero,
                            hero_score=_rating(top_player_score) if hero else "", hero_photo=top_player_photo)


def build_match_dna_share_card(*, home_team: str, away_team: str, home_score: int, away_score: int,
                               drama_level: str, drama_index: float, hero_name: str, hero_score: float | None,
                               headline: str, antihero_name: str = "", antihero_score: float | None = None,
                               fan_mood_text: str = "", consensus_text: str = "", home_logo: str = "",
                               away_logo: str = "") -> str:
    return cards.dna_card(home=home_team, away=away_team, score=f"{home_score}:{away_score}", headline=headline,
                          drama_level=drama_level, drama_index=drama_index or 0,
                          hero=(hero_name, _rating(hero_score)) if hero_name else ("", ""),
                          antihero=(antihero_name, _rating(antihero_score)) if antihero_name else ("", ""),
                          mood=fan_mood_text, consensus=consensus_text, home_logo=home_logo, away_logo=away_logo)


def build_streak_share_card(*, username: str, streak_type: str, streak_count: int) -> str:
    """streak_type: 'evaluation' (туров подряд) или 'prediction' (угаданных подряд)."""
    from core.templatetags.ui_extras import ru_plural

    if streak_type == "evaluation":
        chip, label, sub, accent = "СЕРИЯ ОЦЕНОК", f'{ru_plural(streak_count, "тур,тура,туров")} подряд', "оценивает каждый матч", "blue"
    else:
        chip, label, sub, accent = "СЕРИЯ ПРОГНОЗОВ", f'{ru_plural(streak_count, "прогноз,прогноза,прогнозов")} подряд', "угадал исход матча", "violet"
    return cards.stat_card(prefix="streak", accent_name=accent, chip=chip, number=str(streak_count), label=label,
                           sub=sub, footer_note=f"@{username} на DOPX", progress=min(streak_count / 10, 1))


BRAG_ACCENTS = {"fan_top": "green", "season": "amber", "day_streak": "orange", "predictions": "blue"}


def build_brag_share_card(*, username: str, kind: str, number_text: str, label_line1: str,
                          label_line2: str, eyebrow: str, image_url: str = "") -> str:
    """«Похвастаться»: топ-% болельщиков клуба, уровень сезона, серия дней, сбывшиеся прогнозы."""
    return cards.stat_card(prefix="brag", accent_name=BRAG_ACCENTS.get(kind, "violet"), chip=eyebrow.upper(),
                           number=number_text, label=label_line1, sub=label_line2, footer_note=f"@{username} на DOPX",
                           image_url=image_url)


SOCIAL_ACCENTS = {"player_of_round": "green", "referee": "red", "drama": "orange"}


def build_social_card(*, kind: str, eyebrow: str, number_text: str, label_line1: str, label_line2: str,
                      image_url: str = "") -> str:
    """Картинка для соцсетей и постов канала (игрок тура, спорный судья, драма тура)."""
    return cards.stat_card(prefix="social", accent_name=SOCIAL_ACCENTS.get(kind, "violet"), chip=eyebrow.upper(),
                           number=number_text, label=label_line1, sub=label_line2, image_url=image_url)


def build_round_squad_share_card(*, season_year: str, tour: int, player_of_round_name: str,
                                 player_of_round_score: float | None, dramatic_match_label: str,
                                 player_photo: str = "") -> str:
    return cards.round_card(season=season_year, tour=tour, player=player_of_round_name or "—",
                            score=_rating(player_of_round_score), drama=dramatic_match_label, photo_url=player_photo)


def build_player_season_recap_card(*, player_name: str, team_name: str, season_label: str, matches_played: int,
                                   avg_performance: float | None, goals: int, photo_url: str = "",
                                   team_logo: str = "") -> str:
    return cards.recap_card(player=player_name, team=team_name, season=season_label, matches=matches_played,
                            rating=_rating(avg_performance), goals=goals, photo_url=photo_url, team_logo=team_logo)


def build_badge_share_card(*, username: str, badge_code: str, badge_name: str, badge_description: str,
                           rarity: str, is_secret: bool, awarded_at) -> str:
    """Достижение: огранённый камень в цвет редкости, чем реже — тем больше блеска."""
    return cards.badge_card(username=username, name=badge_name, description=badge_description, rarity=rarity,
                            secret=is_secret, date_label=awarded_at.strftime("%d.%m.%Y") if awarded_at else "")
