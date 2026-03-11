from django.db import models
from core.models import BaseModel


class League(BaseModel):

    name = models.CharField(max_length=255)
    country = models.CharField(max_length=255)

    logo = models.ImageField(upload_to="leagues/", null=True, blank=True)
    
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.name