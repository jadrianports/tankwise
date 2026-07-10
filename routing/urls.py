from django.urls import path

from routing.views import RouteView

urlpatterns = [
    path("route", RouteView.as_view(), name="route"),
]
