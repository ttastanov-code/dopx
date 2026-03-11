from django.db import models
from core.models import BaseModel
from leagues.models import League


class Season(BaseModel):

    league = models.ForeignKey(
        League,
        on_delete=models.CASCADE,
        related_name="seasons"
    )

    year = models.CharField(max_length=20)

    is_active = models.BooleanField(default=False)
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.league.name} {self.year}"