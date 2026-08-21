# partners/urls.py
from django.urls import path

from . import views

app_name = "partners"

urlpatterns = [
    path("go/<slug:slug>/", views.PartnerReferralRedirectView.as_view(), name="referral_redirect"),
    path("ad/<uuid:pk>/click/", views.BannerClickRedirectView.as_view(), name="banner_click"),
    path("partners/<slug:slug>/feed/<uuid:token>/", views.PartnerContentFeedView.as_view(), name="content_feed"),
]
