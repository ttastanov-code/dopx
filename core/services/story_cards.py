# core/services/story_cards.py
"""Вертикальные карточки 1080×1920 для сторис; рисует core.cards (templates/cards/story.svg)."""
from __future__ import annotations

from core import cards

ACCENTS = {"fan_top": "green", "season": "amber", "day_streak": "orange", "predictions": "blue",
           "match": "violet", "prediction_hit": "blue"}


def build_story_card(*, kind: str, eyebrow: str, number_text: str, label_line1: str, label_line2: str = "",
                     footer_note: str = "", image_url: str = "") -> str:
    """eyebrow — рубрика, number_text — главное число, label — подпись, image_url — фото игрока в кольце."""
    return cards.story(accent_name=ACCENTS.get(kind, "violet"), chip=eyebrow.upper(), number=number_text,
                       label=label_line1, sub=label_line2, footer_note=footer_note, image_url=image_url)
