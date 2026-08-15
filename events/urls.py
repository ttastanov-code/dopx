# events/urls.py
from django.urls import path

from .views import pulse_partial, react_to_event

app_name = 'events'

urlpatterns = [
    path('matches/<uuid:match_id>/pulse/', pulse_partial, name='pulse'),
    path('<uuid:event_id>/react/', react_to_event, name='react'),
]
