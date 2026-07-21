from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

from routing.views import ConfigView, HealthView, ReadyView, RouteView

urlpatterns = [
    path("route", RouteView.as_view(), name="route"),
    path("health", HealthView.as_view(), name="health"),
    path("ready", ReadyView.as_view(), name="ready"),
    path("config", ConfigView.as_view(), name="config"),
    # Interactive API docs. None of the three declares
    # throttle_classes, so -- exactly like HealthView/ReadyView/ConfigView
    # above -- they are exempt from throttling by construction.
    path("schema", SpectacularAPIView.as_view(), name="schema"),
    path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("redoc", SpectacularRedocView.as_view(url_name="schema"), name="redoc"),
]
