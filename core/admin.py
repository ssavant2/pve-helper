from django.contrib import admin

from .models import (
    AuditEvent,
    FileInventory,
    OidcIdentity,
    ProxmoxEndpoint,
    ProxmoxInventory,
    ProxmoxStorageConsumer,
    ScanRun,
    StorageMount,
    TrashItem,
)


@admin.register(OidcIdentity)
class OidcIdentityAdmin(admin.ModelAdmin):
    list_display = ("user", "issuer", "subject", "created_at", "updated_at")
    search_fields = ("user__username", "user__email", "issuer", "subject")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "username", "action", "object_type", "object_id", "storage_id", "path", "outcome")
    list_filter = ("action", "outcome", "object_type", "storage_id")
    search_fields = ("username", "action", "object_id", "storage_id", "path")
    readonly_fields = ("timestamp", "storage_id", "path", "target_preallocation")


@admin.register(ProxmoxEndpoint)
class ProxmoxEndpointAdmin(admin.ModelAdmin):
    list_display = ("name", "url", "enabled", "last_health_status", "last_successful_scan")
    list_filter = ("enabled", "last_health_status")
    search_fields = ("name", "url")


@admin.register(StorageMount)
class StorageMountAdmin(admin.ModelAdmin):
    list_display = ("display_name", "storage_id", "path", "expected_consumers", "enabled")
    list_filter = ("enabled",)
    search_fields = ("display_name", "storage_id", "path", "export")


@admin.register(ScanRun)
class ScanRunAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "target_label", "progress_message", "created_at", "started_at", "finished_at")
    list_filter = ("status", "target_storage")
    search_fields = ("queued_task_id", "progress_message")
    readonly_fields = ("created_at", "updated_at", "summary_counts", "error_details", "storage_gate_status")


@admin.register(FileInventory)
class FileInventoryAdmin(admin.ModelAdmin):
    list_display = ("storage", "path", "content_category", "classification", "size_bytes", "modified_at")
    list_filter = ("classification", "content_category", "entry_type", "storage")
    search_fields = ("path", "derived_volid", "classification_reason")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ProxmoxInventory)
class ProxmoxInventoryAdmin(admin.ModelAdmin):
    list_display = ("scan_run", "node", "object_type", "vmid", "name", "status", "disk_reference_count")
    list_filter = ("node", "object_type", "status")
    search_fields = ("node", "name", "vmid", "disk_references")
    readonly_fields = ("created_at", "updated_at")

    @admin.display(description="Disk refs")
    def disk_reference_count(self, obj):
        return len(obj.disk_references or [])


@admin.register(ProxmoxStorageConsumer)
class ProxmoxStorageConsumerAdmin(admin.ModelAdmin):
    list_display = ("storage", "expected_node_name", "last_gate_status", "last_successful_inventory_scan")
    list_filter = ("last_gate_status",)
    search_fields = ("storage__storage_id", "expected_node_name")


@admin.register(TrashItem)
class TrashItemAdmin(admin.ModelAdmin):
    list_display = ("original_path", "restore_status", "moved_by", "moved_at")
    list_filter = ("restore_status",)
    search_fields = ("original_path", "trash_path")
