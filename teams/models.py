from django.db import models
from core.models import BaseModel
# from leagues.models import League
from seasons.models import Season


class Team(BaseModel):

    name = models.CharField(max_length=255)

    # league = models.ForeignKey(
    #     League,
    #     on_delete=models.CASCADE,
    #     related_name="teams"
    # )

    logo = models.ImageField(upload_to="teams/", null=True, blank=True)
    logo_url = models.URLField(
        null=True,
        blank=True
    )
    city = models.CharField(max_length=120, blank=True)
    external_id = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True
    )

    def __str__(self):
        return self.name
    
    
class TeamSeason(BaseModel):

    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE
    )

    season = models.ForeignKey(
        Season,
        on_delete=models.CASCADE
    )