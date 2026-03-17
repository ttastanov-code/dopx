from django.db import models
from core.models import BaseModel

from matches.models import Match
from players.models import Player


class MatchEvent(BaseModel):

    EVENT_TYPES = [
        ("goal", "Goal"),
        ("yellow_card", "Yellow Card"),
        ("red_card", "Red Card"),
        ("substitution", "Substitution"),
    ]

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="events"
    )

    player = models.ForeignKey(
        Player,
        on_delete=models.SET_NULL,
        null=True
    )

    minute = models.IntegerField()

    event_type = models.CharField(
        max_length=30,
        choices=EVENT_TYPES
    )

    team_side = models.CharField(
        max_length=10,
        choices=[
            ("home", "Home"),
            ("away", "Away")
        ]
    )

    external_id = models.CharField(
        max_length=100,
        unique=True
    )

    def __str__(self):
        return f"{self.event_type} {self.minute}"