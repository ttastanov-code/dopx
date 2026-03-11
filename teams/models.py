from django.db import models
from core.models import BaseModel
from leagues.models import League


class Team(BaseModel):

    name = models.CharField(max_length=255)

    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name="teams"
    )

    logo = models.ImageField(upload_to="teams/", null=True, blank=True)

    city = models.CharField(max_length=120, blank=True)
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.name