from django.contrib.auth.models import AbstractUser
from django.db import models
from core.models import BaseModel


class User(AbstractUser, BaseModel):

    email = models.EmailField(unique=True)

    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    bio = models.TextField(blank=True)

    city = models.CharField(max_length=120, blank=True)

    rating_power = models.FloatField(default=1.0)
    trust_score = models.FloatField(default=1.0)

    is_verified = models.BooleanField(default=False)

    def __str__(self):
        return self.username