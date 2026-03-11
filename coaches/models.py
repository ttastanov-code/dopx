from django.db import models
from core.models import BaseModel
from teams.models import Team


class Coach(BaseModel):

    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120)

    team = models.ForeignKey(
        Team,
        on_delete=models.SET_NULL,
        null=True,
        related_name="coaches"
    )

    is_active = models.BooleanField(default=True)
    
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"