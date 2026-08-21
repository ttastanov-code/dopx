# predictions/urls.py
from django.urls import path

from .views import predict, prediction_widget_partial

app_name = 'predictions'

urlpatterns = [
    path('matches/<uuid:match_id>/widget/', prediction_widget_partial, name='widget'),
    path('matches/<uuid:match_id>/predict/', predict, name='predict'),
]
