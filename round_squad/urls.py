# round_squad/urls.py
from django.urls import path

from round_squad import views

app_name = 'round_squad'

urlpatterns = [
    # Без season_id/tour — последний завершённый тур активного сезона
    # главной лиги (тот же принцип "умолчания", что у season_squad:best_xi).
    path('round/', views.round_of_week, name='round'),
    path('round/partial/', views.round_of_week_partial, name='round_partial'),
    path('<uuid:season_id>/round/<int:tour>/', views.round_of_week, name='round'),
    path('<uuid:season_id>/round/<int:tour>/partial/', views.round_of_week_partial, name='round_partial'),
    # Embed-виджет «DOPX Лучшие тура» для чужих сайтов (тот же паттерн, что
    # season_squad:widget) — см. views.py::round_widget и
    # dopx/middleware.py::WIDGET_PATH_PATTERN (CSP frame-ancestors).
    path('round/widget/', views.round_widget, name='round_widget'),
    path('<uuid:season_id>/round/<int:tour>/widget/', views.round_widget, name='round_widget'),
]
