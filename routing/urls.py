from django.urls import path

from routing.views import HealthView, ReadyView, RouteView

urlpatterns = [
    path("route", RouteView.as_view(), name="route"),
    path("health", HealthView.as_view(), name="health"),
    path("ready", ReadyView.as_view(), name="ready"),
]
