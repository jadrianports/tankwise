from django.urls import include, path, re_path

from routing.views import SpaFallbackView

urlpatterns = [
    path("api/", include("routing.urls")),
    # D-08 URL layout: the SPA owns the root, the backend stays namespaced
    # under /api/, and collectstatic output is served at /static/. This
    # catch-all must stay last so it can never shadow the api/ include, and
    # the negative lookahead excludes both the api/ and static/ prefixes so
    # an unknown /api/ path still returns a real Django 404 instead of the
    # SPA shell.
    re_path(r"^(?!api/|static/).*$", SpaFallbackView.as_view()),
]
