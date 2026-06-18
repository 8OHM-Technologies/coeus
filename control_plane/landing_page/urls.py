from django.urls import path
from .views import LandingPageView, OfflineView, ServiceWorkerView

app_name = "landing_page"

urlpatterns = [
    path("", LandingPageView.as_view(), name="index"),
    path("offline/", OfflineView.as_view(), name="offline"),
    path("sw.js", ServiceWorkerView.as_view(), name="sw"),
]
