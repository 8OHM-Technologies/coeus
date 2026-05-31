# control_plane/pipelines/urls.py
from django.urls import path

from .views import active_pipelines_api

urlpatterns = [
    path("active/", active_pipelines_api, name="active-pipelines-api"),
]
