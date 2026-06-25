from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("datastores/", views.datastores, name="datastores"),
    path("orphans/", views.orphan_finder, name="orphan_finder"),
    path("audit/", views.audit_log, name="audit_log"),
    path("scans/start/", views.start_scan, name="start_scan"),
    path("healthz/live", views.health_live, name="health_live"),
    path("healthz/ready", views.health_ready, name="health_ready"),
]
