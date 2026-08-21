from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models import MatchPrediction


@admin.register(MatchPrediction)
class MatchPredictionAdmin(ModelAdmin):
    list_display = ("match", "user", "choice", "created_at")
    list_filter = ("choice",)
    autocomplete_fields = ("user",)
    raw_id_fields = ("match",)
    actions = [export_as_csv]
