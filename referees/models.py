from django.db import models
from core.models import BaseModel


class Referee(BaseModel):

    first_name = models.CharField(max_length=120)
    last_name = models.CharField(max_length=120)

    country = models.CharField(max_length=120, blank=True)

    is_active = models.BooleanField(default=True)
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return f"{self.first_name} {self.last_name}"