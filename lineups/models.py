from django.db import models
from core.models import BaseModel

from matches.models import Match
from teams.models import Team
from players.models import Player


class MatchLineup(BaseModel):

    match = models.ForeignKey(
        Match,
        on_delete=models.CASCADE,
        related_name="lineups"
    )

    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE
    )

    side = models.CharField(
        max_length=10,
        choices=[
            ("home", "Home"),
            ("away", "Away"),
        ]
    )

    formation = models.CharField(
        max_length=20,
        blank=True
    )

    def __str__(self):
        return f"{self.match} {self.team}"


class MatchLineupPlayer(BaseModel):

    lineup = models.ForeignKey(
        MatchLineup,
        on_delete=models.CASCADE,
        related_name="players"
    )

    player = models.ForeignKey(
        Player,
        on_delete=models.CASCADE
    )

    is_starting = models.BooleanField(default=True)

    position = models.CharField(
        max_length=20,
        blank=True
    )

    shirt_number = models.IntegerField(
        null=True,
        blank=True
    )

    minute_in = models.IntegerField(
        null=True,
        blank=True
    )

    minute_out = models.IntegerField(
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.player}"