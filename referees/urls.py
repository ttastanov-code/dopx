# referees/urls.py
from django.urls import path
from referees.views import RefereeListView, RefereeDetailView

app_name = 'referees'

urlpatterns = [
    path('', RefereeListView.as_view(), name='list'),
    path('<uuid:pk>/', RefereeDetailView.as_view(), name='detail'),
]