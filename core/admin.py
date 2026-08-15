from django.contrib import admin
from unfold.admin import ModelAdmin

from core.admin_actions import export_as_csv

from .models_stadium import Stadium

@admin.register(Stadium)
class StadiumAdmin(ModelAdmin):
    list_display = ('name', 'city', 'capacity')
    search_fields = ('name', 'city')
    actions = [export_as_csv]