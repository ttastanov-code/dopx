from django.db import models
from core.models import BaseModel


class Stadium(BaseModel):

    name = models.CharField(max_length=255)
    city = models.CharField(max_length=255, blank=True)

    capacity = models.IntegerField(null=True, blank=True)

    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.name