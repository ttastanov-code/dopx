# core/urls.py
from django.urls import path
from .views import HomeView, RulesView, ContactsView, standings_preview, handler_404, handler_500

app_name = 'core'

urlpatterns = [
    path('', HomeView.as_view(), name='home'),
    path('rules/', RulesView.as_view(), name='rules'),
    path('contacts/', ContactsView.as_view(), name='contacts'),
    path('api/standings-preview/', standings_preview, name='standings_preview'),
]