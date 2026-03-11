from django.db import models
from core.models import BaseModel
from teams.models import Team


class Player(BaseModel):

    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120)

    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        related_name="players"
    )

    position = models.CharField(max_length=50)

    number = models.IntegerField(null=True, blank=True)

    photo = models.ImageField(upload_to="players/", null=True, blank=True)

    is_active = models.BooleanField(default=True)
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"