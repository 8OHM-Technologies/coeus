# control_plane/pipelines/urls.py
from django.urls import path

from .views import active_pipelines_api, pipeline_analytics_api

urlpatterns = [
    path("active/", active_pipelines_api, name="active-pipelines-api"),
    path("analytics/", pipeline_analytics_api, name="pipeline-analytics-api"),
]
