from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("datastores/", views.datastores, name="datastores"),
    path("storage/<str:storage_id>/browser/", views.storage_browser, name="storage_browser"),
    path("storage/<str:storage_id>/download/", views.download_storage_file, name="storage_download"),
    path("orphans/", views.orphan_finder, name="orphan_finder"),
    path("audit/", views.audit_log, name="audit_log"),
    path("tasks/recent/", views.recent_tasks, name="recent_tasks"),
    path("scans/schedule/", views.update_scan_schedule_view, name="update_scan_schedule"),
    path("scans/status/", views.scan_status, name="scan_status"),
    path("scans/start/", views.start_scan, name="start_scan"),
    path("healthz/live", views.health_live, name="health_live"),
    path("healthz/ready", views.health_ready, name="health_ready"),
]
