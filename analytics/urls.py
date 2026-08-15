from django.urls import path

from analytics.views import TrackClientEventView

app_name = "analytics"

urlpatterns = [path("track/", TrackClientEventView.as_view(), name="track")]
