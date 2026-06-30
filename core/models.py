from __future__ import annotations

from django.conf import settings
from django.db import models


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class OidcIdentity(TimestampedModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="pve_helper_oidc_identities",
    )
    issuer = models.CharField(max_length=512)
    subject = models.CharField(max_length=255)

    class Meta:
        ordering = ["issuer", "subject"]
        constraints = [
            models.UniqueConstraint(fields=["issuer", "subject"], name="unique_oidc_identity_subject"),
        ]
        indexes = [
            models.Index(fields=["user"], name="core_oidcid_user_id_idx"),
            models.Index(fields=["issuer", "subject"], name="core_oidcid_issuer_subject_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.issuer}:{self.subject}"


class AuditEvent(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pve_helper_audit_events",
    )
    username = models.CharField(max_length=255, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    action = models.CharField(max_length=120)
    object_type = models.CharField(max_length=120, blank=True)
    object_id = models.CharField(max_length=512, blank=True)
    outcome = models.CharField(max_length=60, default="success")
    storage_id = models.CharField(max_length=120, blank=True)
    path = models.CharField(max_length=1024, blank=True)
    target_preallocation = models.CharField(max_length=40, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["action", "outcome"]),
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["storage_id", "timestamp"], name="core_audit_store_time_idx"),
            models.Index(fields=["storage_id", "path", "target_preallocation"], name="core_audit_store_path_pre_idx"),
        ]

    def save(self, *args, **kwargs):
        self.populate_filter_fields_from_details()
        super().save(*args, **kwargs)

    def populate_filter_fields_from_details(self) -> None:
        details = self.details if isinstance(self.details, dict) else {}
        self.storage_id = _details_text(details, "storage_id", 120)
        self.path = _details_text(details, "path", 1024)
        self.target_preallocation = _details_text(details, "target_preallocation", 40)

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.action} {self.outcome}"


def _details_text(details: dict, key: str, max_length: int) -> str:
    value = details.get(key, "")
    if value is None or isinstance(value, (dict, list, tuple)):
        return ""
    return str(value)[:max_length]


class ProxmoxEndpoint(TimestampedModel):
    name = models.CharField(max_length=120, unique=True)
    url = models.URLField()
    enabled = models.BooleanField(default=True)
    last_health_status = models.CharField(max_length=60, blank=True)
    last_successful_scan = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class StorageMount(TimestampedModel):
    storage_id = models.CharField(max_length=120, unique=True)
    display_name = models.CharField(max_length=160)
    export = models.CharField(max_length=512, blank=True)
    path = models.CharField(max_length=512)
    trash_path = models.CharField(max_length=512, blank=True)
    expected_consumers = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.storage_id})"


class ScanRun(TimestampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    queued_task_id = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.QUEUED)
    progress_message = models.CharField(max_length=255, blank=True)
    endpoints_attempted = models.JSONField(default=list, blank=True)
    endpoints_succeeded = models.JSONField(default=list, blank=True)
    summary_counts = models.JSONField(default=dict, blank=True)
    error_details = models.JSONField(default=dict, blank=True)
    storage_gate_status = models.JSONField(default=dict, blank=True)
    filesystem_scan_at = models.DateTimeField(null=True, blank=True)
    proxmox_inventory_at = models.DateTimeField(null=True, blank=True)
    target_storage = models.ForeignKey(
        StorageMount,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="scan_runs",
    )
    target_label = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Scan {self.pk or 'new'} ({self.status})"


class FileInventory(TimestampedModel):
    class EntryType(models.TextChoices):
        FILE = "file", "File"
        DIRECTORY = "directory", "Directory"
        SYMLINK = "symlink", "Symlink"
        OTHER = "other", "Other"

    class Classification(models.TextChoices):
        REFERENCED = "referenced", "Referenced"
        LIKELY_ORPHAN = "likely_orphan", "Likely orphan"
        UNKNOWN = "unknown", "Unknown"
        CLASSIFICATION_BLOCKED = "classification_blocked", "Classification blocked"
        TRASH = "trash", "Trash"
        INFRASTRUCTURE = "infrastructure", "Infrastructure"
        PROXMOX_CONTENT = "proxmox_content", "Proxmox content"

    scan_run = models.ForeignKey(ScanRun, on_delete=models.CASCADE, related_name="files")
    storage = models.ForeignKey(StorageMount, on_delete=models.CASCADE, related_name="files")
    path = models.CharField(max_length=1024)
    derived_volid = models.CharField(max_length=512, blank=True)
    content_category = models.CharField(max_length=80, blank=True)
    entry_type = models.CharField(max_length=30, choices=EntryType.choices, default=EntryType.FILE)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    modified_at = models.DateTimeField(null=True, blank=True)
    classification = models.CharField(
        max_length=40,
        choices=Classification.choices,
        default=Classification.UNKNOWN,
    )
    classification_reason = models.TextField(blank=True)
    matched_object = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["storage__display_name", "path"]
        indexes = [
            models.Index(fields=["storage", "path"]),
            models.Index(fields=["storage", "derived_volid"]),
            models.Index(fields=["classification"]),
            models.Index(fields=["content_category"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["scan_run", "storage", "path"],
                name="unique_file_inventory_per_scan_storage_path",
            )
        ]

    def __str__(self) -> str:
        return self.path


class ProxmoxInventory(TimestampedModel):
    class ObjectType(models.TextChoices):
        VM = "vm", "VM"
        CT = "ct", "Container"
        STORAGE = "storage", "Storage"
        NODE = "node", "Node"

    scan_run = models.ForeignKey(ScanRun, on_delete=models.CASCADE, related_name="proxmox_objects")
    node = models.CharField(max_length=120, db_index=True)
    object_type = models.CharField(max_length=30, choices=ObjectType.choices)
    vmid = models.IntegerField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=80, blank=True)
    config = models.JSONField(default=dict, blank=True)
    disk_references = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["node", "object_type", "vmid"]
        indexes = [
            models.Index(fields=["scan_run", "node"]),
            models.Index(fields=["object_type", "vmid"]),
        ]

    def __str__(self) -> str:
        label = self.name or self.vmid or self.object_type
        return f"{self.node}: {label}"


class ProxmoxStorageConsumer(TimestampedModel):
    storage = models.ForeignKey(
        StorageMount,
        on_delete=models.CASCADE,
        related_name="consumer_statuses",
    )
    expected_node_name = models.CharField(max_length=120)
    last_successful_inventory_scan = models.DateTimeField(null=True, blank=True)
    last_gate_status = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["storage__display_name", "expected_node_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["storage", "expected_node_name"],
                name="unique_storage_expected_consumer",
            )
        ]

    def __str__(self) -> str:
        return f"{self.storage.storage_id}: {self.expected_node_name}"


class TrashItem(TimestampedModel):
    class RestoreStatus(models.TextChoices):
        TRASHED = "trashed", "Trashed"
        RESTORED = "restored", "Restored"
        PURGED = "purged", "Purged"
        FAILED = "failed", "Failed"

    original_path = models.CharField(max_length=1024)
    trash_path = models.CharField(max_length=1024)
    storage_id = models.CharField(max_length=120, blank=True)
    moved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pve_helper_trash_items",
    )
    moved_at = models.DateTimeField(null=True, blank=True)
    restore_status = models.CharField(
        max_length=40,
        choices=RestoreStatus.choices,
        default=RestoreStatus.TRASHED,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["storage_id", "restore_status", "moved_at"], name="core_trash_store_status_idx"),
        ]

    def __str__(self) -> str:
        return self.original_path

    def save(self, *args, **kwargs):
        if not self.storage_id and isinstance(self.metadata, dict):
            self.storage_id = _details_text(self.metadata, "storage_id", 120)
        super().save(*args, **kwargs)


class StorageSpaceSnapshot(TimestampedModel):
    storage = models.ForeignKey(StorageMount, on_delete=models.CASCADE, related_name="space_snapshots")
    scan_run = models.ForeignKey(
        ScanRun,
        on_delete=models.CASCADE,
        related_name="space_snapshots",
        null=True,
        blank=True,
    )
    recorded_at = models.DateTimeField()
    total_bytes = models.BigIntegerField()
    available_bytes = models.BigIntegerField()
    used_bytes = models.BigIntegerField()

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [
            models.Index(fields=["storage", "recorded_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.storage.storage_id} @ {self.recorded_at:%Y-%m-%d %H:%M}"
