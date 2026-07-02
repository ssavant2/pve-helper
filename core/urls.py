from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("datastores/", views.datastores, name="datastores"),
    path("storage/<str:storage_id>/summary/", views.storage_summary, name="storage_summary"),
    path("storage/<str:storage_id>/monitor/", views.storage_monitor, name="storage_monitor"),
    path("storage/<str:storage_id>/configure/", views.storage_configure, name="storage_configure"),
    path("storage/<str:storage_id>/permissions/", views.storage_permissions_view, name="storage_permissions"),
    path("storage/<str:storage_id>/browser/", views.storage_browser, name="storage_browser"),
    path("storage/<str:storage_id>/hosts/", views.storage_hosts, name="storage_hosts"),
    path("storage/<str:storage_id>/vms/", views.storage_vms, name="storage_vms"),
    path("storage/<str:storage_id>/download/", views.download_storage_file, name="storage_download"),
    path("storage/<str:storage_id>/create-folder/", views.create_storage_folder, name="storage_create_folder"),
    path("storage/<str:storage_id>/upload/", views.upload_storage_file, name="storage_upload"),
    path("storage/<str:storage_id>/upload-folder/", views.upload_storage_folder, name="storage_upload_folder"),
    path("storage/<str:storage_id>/trash/", views.storage_trash, name="storage_trash"),
    path("storage/<str:storage_id>/trash-file/", views.trash_storage_file, name="storage_trash_file"),
    path("storage/<str:storage_id>/move-file/", views.move_storage_file_view, name="storage_move_file"),
    path("storage/<str:storage_id>/rename-file/", views.rename_storage_file_view, name="storage_rename_file"),
    path("storage/<str:storage_id>/inflate-file/", views.inflate_storage_file_view, name="storage_inflate_file"),
    path("trash/<int:trash_item_id>/restore/", views.restore_storage_file, name="storage_restore_file"),
    path("trash/<int:trash_item_id>/purge/", views.purge_trash_item, name="purge_trash_item"),
    path("orphans/", views.orphan_finder, name="orphan_finder"),
    path("scheduled-tasks/", views.scheduled_tasks, name="scheduled_tasks"),
    path("scheduled-tasks/new/", views.scheduled_task_create, name="scheduled_task_create"),
    path("scheduled-tasks/<int:action_id>/edit/", views.scheduled_task_edit, name="scheduled_task_edit"),
    path("scheduled-tasks/<int:action_id>/toggle/", views.scheduled_task_toggle, name="scheduled_task_toggle"),
    path("scheduled-tasks/<int:action_id>/delete/", views.scheduled_task_delete, name="scheduled_task_delete"),
    path("scheduled-tasks/<int:action_id>/run-now/", views.scheduled_task_run_now, name="scheduled_task_run_now"),
    path("audit/", views.audit_log, name="audit_log"),
    path("tasks/recent/", views.recent_tasks, name="recent_tasks"),
    path("scans/schedule/", views.update_scan_schedule_view, name="update_scan_schedule"),
    path("trash/purge-schedule/", views.update_trash_purge_schedule_view, name="update_trash_purge_schedule"),
    path("audit/retention-schedule/", views.update_audit_retention_schedule_view, name="update_audit_retention_schedule"),
    path("scans/status/", views.scan_status, name="scan_status"),
    path("scans/start/", views.start_scan, name="start_scan"),
    path("healthz/live", views.health_live, name="health_live"),
    path("healthz/ready", views.health_ready, name="health_ready"),
]
