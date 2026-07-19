from __future__ import annotations

import uuid

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower

# A dependency-free value object: refs.py must never import models, or this cycles.
from core.services.refs import GuestRef, NodeRef


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
    # Denormalized UI category (auth/vms/storage/clusters/network/system) so the
    # audit-log module filter can query the DB instead of only the rendered page.
    module = models.CharField(max_length=20, blank=True, db_index=True)
    # Durable scope snapshot plus relation. The snapshot remains meaningful when
    # the cluster display name changes; the relation supports filtering and
    # prevents deleting a cluster that still owns history.
    cluster = models.ForeignKey(
        "ProxmoxCluster",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    cluster_key_snapshot = models.CharField(max_length=63, blank=True)
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
            models.Index(fields=["cluster_key_snapshot", "timestamp"], name="core_audit_cluster_time_idx"),
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


RUNTIME_CONFIGURATION_SINGLETON_PK = 1

cluster_key_validator = RegexValidator(
    regex=r"^[a-z0-9][a-z0-9-]{0,62}$",
    message=(
        "Cluster key must be lowercase and URL-safe: it may contain a-z, 0-9 and hyphens, "
        "must start with a letter or digit, and may be at most 63 characters."
    ),
)


class ProxmoxCluster(TimestampedModel):
    """An independent Proxmox cluster. Durable guest identity is (cluster.key, object_type, vmid).

    The key is operator-controlled and immutable once cluster-qualified contracts
    activate; an endpoint is a transport for this cluster, never its identity. The
    discovered_* fields corroborate the binding and must never define it.
    """

    key = models.CharField(max_length=63, validators=[cluster_key_validator])
    display_name = models.CharField(max_length=160)
    enabled = models.BooleanField(default=True)
    # The pinned identity binding. `discovered_ca_uuid` is the identity claim (the
    # cluster CA's UUID), `discovered_ca_fingerprint` the trust anchor pinned on
    # first approval, `discovered_name` mutable corroboration. Identity is still
    # `key`; these confirm that an endpoint still speaks for the cluster it claims.
    discovered_name = models.CharField(max_length=255, blank=True)
    discovered_ca_uuid = models.CharField(max_length=64, blank=True)
    discovered_ca_fingerprint = models.CharField(max_length=200, blank=True)
    # Ingestion halts when an endpoint reports a different cluster CA than the one
    # pinned: a re-pointed or restored endpoint would otherwise merge another
    # cluster's guests under this key. Cleared only by explicit re-approval.
    ingestion_quarantined = models.BooleanField(default=False)
    quarantine_reason = models.CharField(max_length=255, blank=True)
    quarantined_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    # Cache keys include this generation. Bumping it invalidates only this
    # cluster's process-local cache entries across every web/worker process.
    cache_generation = models.PositiveBigIntegerField(default=1)

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                Lower("key"),
                name="unique_cluster_key_case_insensitive",
            ),
            models.UniqueConstraint(
                fields=["discovered_ca_uuid"],
                condition=~models.Q(discovered_ca_uuid=""),
                name="unique_nonblank_cluster_ca_uuid",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.key})"


class ClusterCredential(TimestampedModel):
    """The API token pve-helper authenticates to one cluster with.

    The credential belongs to the cluster and is shared by its endpoints, which are
    alternative transports to the same control plane. Endpoint-specific credentials
    would need an explicit use case rather than becoming an accidental second
    convention.

    The secret is only ever stored sealed. `encryption_key_id` duplicates the key id
    that the ciphertext already names, so rotation can find rows sealed under an old
    key, and startup can check that every referenced key is present, without
    decrypting anything.
    """

    cluster = models.OneToOneField(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="credential",
    )
    # Not a secret: an identifier like `pve-helper@pve!pve-helper`, shown in the UI
    # and in audit so an operator can tell which token is in use.
    token_id = models.CharField(max_length=255)
    token_secret_sealed = models.TextField()
    encryption_key_id = models.CharField(max_length=64, db_index=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["cluster__key"]

    def __str__(self) -> str:
        return f"credential for {self.cluster.key} ({self.token_id})"


class ClusterTransportTrust(TimestampedModel):
    """How this cluster's TLS certificate chain is trusted.

    Deliberately separate from the identity binding on ProxmoxCluster: transport
    trust answers which chain the HTTP client accepts, identity binding answers
    which cluster an authenticated endpoint belongs to. They are often the same PVE
    CA, but not when pveproxy serves a publicly trusted certificate while the
    internal cluster CA remains the identity claim.

    `PVE_CA_BUNDLE` cannot express this: it is one global file outside the database,
    so it cannot say "cluster A trusts CA X, cluster B trusts CA Y" and a UI cannot
    manage it. Trust therefore lives here, per cluster.
    """

    class Mode(models.TextChoices):
        PUBLIC = "public", "Publicly trusted"
        CA_PEM = "ca_pem", "Internal CA bundle"

    cluster = models.OneToOneField(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="transport_trust",
    )
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.PUBLIC)
    # The exclusively trusted CA bundle for CA_PEM mode; empty for PUBLIC.
    ca_pem = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["cluster__key"]

    def __str__(self) -> str:
        return f"transport trust for {self.cluster.key} ({self.mode})"


class RuntimeConfigurationState(TimestampedModel):
    """Singleton recording who owns runtime configuration and how far identity has migrated.

    The database is the source of truth for configuration; environment is a
    bootstrap importer that runs exactly once. This marker is what distinguishes an
    unbootstrapped installation from one an operator deliberately emptied, so it
    must survive deletion of every cluster record.
    """

    SINGLETON_PK = RUNTIME_CONFIGURATION_SINGLETON_PK

    id = models.PositiveSmallIntegerField(primary_key=True, default=RUNTIME_CONFIGURATION_SINGLETON_PK)
    bootstrap_completed = models.BooleanField(default=False)
    bootstrap_completed_at = models.DateTimeField(null=True, blank=True)
    bootstrap_fingerprint = models.CharField(max_length=64, blank=True)
    identity_contract_version = models.PositiveSmallIntegerField(default=0)
    # Phase 1c/1d write these; once set, runtime stops reading the legacy global
    # token/CA settings. They are ignored at cutover, never deleted, so a code
    # rollback resumes reading them and re-import stays idempotent.
    credential_cutover_completed_at = models.DateTimeField(null=True, blank=True)
    trust_cutover_completed_at = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(id=RUNTIME_CONFIGURATION_SINGLETON_PK),
                name="runtime_configuration_state_is_singleton",
            ),
        ]

    def __str__(self) -> str:
        state = "bootstrapped" if self.bootstrap_completed else "unbootstrapped"
        return f"runtime configuration ({state}, identity contract v{self.identity_contract_version})"


class ProxmoxEndpoint(TimestampedModel):
    name = models.CharField(max_length=120)
    url = models.URLField()
    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.PROTECT,
        related_name="endpoints",
    )
    # Canonical form of `url`, kept in sync on save. It exists so the database can
    # enforce that one transport is never claimed by two clusters: an endpoint
    # answering for the wrong cluster would file its inventory under the wrong
    # identity, which is the whole failure this foundation prevents.
    normalized_url = models.CharField(max_length=512, blank=True)
    enabled = models.BooleanField(default=True)
    last_health_status = models.CharField(max_length=60, blank=True)
    last_successful_scan = models.DateTimeField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "name"],
                name="unique_endpoint_name_per_cluster",
                nulls_distinct=False,
            ),
            models.UniqueConstraint(
                fields=["normalized_url"],
                condition=~models.Q(normalized_url=""),
                name="unique_endpoint_normalized_url",
            ),
        ]

    def save(self, *args, **kwargs):
        from core.services.config import normalize_endpoint_url

        self.normalized_url = normalize_endpoint_url(self.url)
        if "update_fields" in kwargs and kwargs["update_fields"] is not None:
            update_fields = set(kwargs["update_fields"])
            if "url" in update_fields:
                update_fields.add("normalized_url")
                kwargs["update_fields"] = sorted(update_fields)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class ConsoleSession(TimestampedModel):
    class TargetType(models.TextChoices):
        VM = "vm", "VM"
        CT = "ct", "Container"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        CONNECTING = "connecting", "Connecting"
        CONNECTED = "connected", "Connected"
        CLOSED = "closed", "Closed"
        FAILED = "failed", "Failed"
        EXPIRED = "expired", "Expired"

    token_hash = models.CharField(max_length=64, unique=True)
    # The cluster this console attaches to. The gateway resolves that cluster's
    # current credential and WSS trust at connect time, so a same-VMID guest on a
    # same-named node elsewhere can never hand the operator the wrong machine's
    # shell. Nullable for the additive migration; legacy sessions have none and the
    # gateway falls back to the global settings for them until they expire.
    cluster = models.ForeignKey(
        "ProxmoxCluster",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="console_sessions",
    )
    target_type = models.CharField(max_length=20, choices=TargetType.choices)
    target_vmid = models.PositiveIntegerField()
    target_node = models.CharField(max_length=120, blank=True)
    target_name_snapshot = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pve_helper_console_sessions",
    )
    username = models.CharField(max_length=255, blank=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING, db_index=True)
    proxmox_endpoint = models.URLField(blank=True)
    proxmox_node = models.CharField(max_length=120, blank=True)
    proxmox_upid = models.CharField(max_length=255, blank=True)
    proxmox_port = models.CharField(max_length=20, blank=True)
    proxmox_ticket = models.TextField(blank=True)
    proxmox_password = models.CharField(max_length=255, blank=True)
    close_reason = models.CharField(max_length=255, blank=True)
    error = models.TextField(blank=True)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_type", "target_vmid"], name="core_console_target_idx"),
            models.Index(fields=["cluster", "target_type", "target_vmid"], name="core_con_cluster_target_idx"),
            models.Index(
                fields=["cluster", "target_node", "target_type", "target_vmid"],
                name="core_con_cluster_node_idx",
            ),
            models.Index(fields=["status", "expires_at"], name="core_console_status_exp_idx"),
        ]

    def guest_ref(self) -> "GuestRef | None":
        if self.cluster_id is None:
            return None
        return GuestRef(
            cluster_key=self.cluster.key,
            object_type=self.target_type,
            vmid=self.target_vmid,
            node=self.target_node,
        )

    def __str__(self) -> str:
        return f"{self.target_type}:{self.target_vmid} console {self.status}"


class StorageMount(TimestampedModel):
    # Legacy display/import hint only. Durable identity is mount_key; PVE storage
    # identity belongs to ClusterStorage and is qualified by cluster.
    storage_id = models.CharField(max_length=120, db_index=True)
    mount_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    display_name = models.CharField(max_length=160)
    export = models.CharField(max_length=512, blank=True)
    path = models.CharField(max_length=512)
    relative_path = models.CharField(max_length=512, blank=True)
    trash_path = models.CharField(max_length=512, blank=True)
    trash_relative_path = models.CharField(max_length=512, blank=True)
    filesystem_type = models.CharField(max_length=40, blank=True)
    backend_identity = models.CharField(max_length=512, blank=True)
    expected_consumers = models.JSONField(default=list, blank=True)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["display_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["relative_path"],
                condition=~models.Q(relative_path=""),
                name="unique_storage_mount_path",
            )
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.storage_id})"

    @property
    def mount_ref(self) -> str:
        from core.services.refs import MountRef

        return MountRef(str(self.mount_key)).serialize()


class StorageCatalogState(TimestampedModel):
    """Independent publication state for cheap metadata and expensive content."""

    cluster = models.OneToOneField(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="storage_catalog_state",
    )
    metadata_generation = models.UUIDField(null=True, blank=True)
    metadata_refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    metadata_last_attempt_at = models.DateTimeField(null=True, blank=True)
    metadata_complete = models.BooleanField(default=False)
    metadata_errors = models.JSONField(default=dict, blank=True)
    volume_generation = models.UUIDField(null=True, blank=True)
    volume_based_on_metadata_generation = models.UUIDField(null=True, blank=True)
    volume_refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    volume_last_attempt_at = models.DateTimeField(null=True, blank=True)
    volume_complete = models.BooleanField(default=False)
    volume_errors = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["cluster__key"]

    def __str__(self) -> str:
        return f"Storage catalog [{self.cluster.key}]"


class ClusterStorage(TimestampedModel):
    """Current Proxmox-authoritative storage definition projection."""

    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.PROTECT,
        related_name="storage_definitions",
    )
    storage_id = models.CharField(max_length=120)
    storage_type = models.CharField(max_length=40)
    content = models.JSONField(default=list, blank=True)
    shared = models.BooleanField(default=False)
    nodes = models.JSONField(default=list, blank=True)
    disabled = models.BooleanField(default=False)
    config = models.JSONField(default=dict, blank=True)
    present = models.BooleanField(default=True)
    retired_at = models.DateTimeField(null=True, blank=True)
    observed_metadata_generation = models.UUIDField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["cluster__key", "storage_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "storage_id"],
                name="unique_cluster_storage_definition",
            )
        ]
        indexes = [
            models.Index(fields=["cluster", "present", "disabled"], name="core_cstorage_state_idx"),
            models.Index(fields=["cluster", "retired_at"], name="core_cstorage_retired_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.cluster.key}/{self.storage_id}"


class ClusterStorageNodeState(TimestampedModel):
    """Current state of one node's access to one cluster storage definition."""

    cluster_storage = models.ForeignKey(
        ClusterStorage,
        on_delete=models.CASCADE,
        related_name="node_states",
    )
    node = models.CharField(max_length=120)
    active = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    total_bytes = models.BigIntegerField(null=True, blank=True)
    used_bytes = models.BigIntegerField(null=True, blank=True)
    available_bytes = models.BigIntegerField(null=True, blank=True)
    present = models.BooleanField(default=True)
    observed_metadata_generation = models.UUIDField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["cluster_storage__storage_id", "node"]
        constraints = [
            models.UniqueConstraint(
                fields=["cluster_storage", "node"],
                name="unique_cluster_storage_node_state",
            )
        ]
        indexes = [
            models.Index(
                fields=["cluster_storage", "present", "active"],
                name="core_csnode_state_idx",
            ),
            models.Index(fields=["node", "active"], name="core_csnode_active_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.cluster_storage}@{self.node}"


class ClusterStorageMount(TimestampedModel):
    """Operator-owned binding from a PVE storage scope to a host mount."""

    class Scope(models.TextChoices):
        SHARED = "shared", "Shared"
        NODE = "node", "Node"

    cluster_storage = models.ForeignKey(
        ClusterStorage,
        on_delete=models.CASCADE,
        related_name="mount_bindings",
    )
    mount = models.ForeignKey(
        StorageMount,
        on_delete=models.PROTECT,
        related_name="cluster_bindings",
    )
    scope = models.CharField(max_length=12, choices=Scope.choices)
    node = models.CharField(max_length=120, null=True, blank=True)

    class Meta:
        ordering = ["cluster_storage__cluster__key", "cluster_storage__storage_id", "node"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(scope="shared", node__isnull=True) | models.Q(scope="node", node__isnull=False)),
                name="storage_mount_scope_matches_node",
            ),
            models.UniqueConstraint(
                fields=["cluster_storage", "node"],
                name="unique_cluster_storage_mount_scope",
                nulls_distinct=False,
            ),
        ]

    def __str__(self) -> str:
        scope = self.node or "shared"
        return f"{self.cluster_storage}@{scope} -> {self.mount.mount_key}"


class ClusterStorageVolumeObservation(TimestampedModel):
    """Mutable current content observation, qualified by the answering node."""

    cluster_storage = models.ForeignKey(
        ClusterStorage,
        on_delete=models.CASCADE,
        related_name="volume_observations",
    )
    node = models.CharField(max_length=120)
    volid = models.CharField(max_length=512)
    vmid = models.PositiveIntegerField(null=True, blank=True)
    content = models.CharField(max_length=40, blank=True)
    volume_format = models.CharField(max_length=40, blank=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    used_bytes = models.BigIntegerField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    observed_volume_generation = models.UUIDField()
    based_on_metadata_generation = models.UUIDField()
    last_seen_at = models.DateTimeField()

    class Meta:
        ordering = ["cluster_storage__storage_id", "node", "volid"]
        constraints = [
            models.UniqueConstraint(
                fields=["cluster_storage", "node", "volid"],
                name="unique_cluster_storage_volume_observation",
            )
        ]
        indexes = [
            models.Index(
                fields=["cluster_storage", "observed_volume_generation"],
                name="core_csvol_generation_idx",
            ),
            models.Index(fields=["vmid"], name="core_csvol_vmid_idx"),
            models.Index(fields=["content"], name="core_csvol_content_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.cluster_storage}@{self.node}: {self.volid}"


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
        IMPORT_SOURCE = "import_source", "Import source"

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
    # Which cluster this scan evidence came from. New rows are always qualified.
    # Nullable only to retain genuinely ambiguous pre-contract history; such rows
    # are display evidence and fail closed if considered by a file action.
    cluster = models.ForeignKey(
        ProxmoxCluster,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proxmox_objects",
    )
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
            models.Index(fields=["cluster", "object_type", "vmid"], name="core_pinv_cluster_type_vmid"),
        ]

    def __str__(self) -> str:
        label = self.name or self.vmid or self.object_type
        return f"{self.node}: {label}"

    def guest_ref(self) -> "GuestRef | None":
        if self.cluster_id is None or self.vmid is None or self.object_type not in {"vm", "ct"}:
            return None
        return GuestRef(self.cluster.key, self.object_type, self.vmid, self.node)


class CurrentGuestInventory(TimestampedModel):
    """Mutable current-state projection for VM/CT reads.

    Historical ``ProxmoxInventory`` rows remain scan evidence. All interactive
    guest/tag consumers use this projection instead.
    """

    class ObjectType(models.TextChoices):
        VM = ProxmoxInventory.ObjectType.VM, "VM"
        CT = ProxmoxInventory.ObjectType.CT, "Container"

    # Durable cluster identity of the guest. A guest is (cluster, object_type, vmid);
    # the source_endpoint below is only where this projection was last observed and
    # may change. The rollout backfilled source_endpoint.cluster where known,
    # otherwise the sole cluster; the activated contract now requires it.
    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.PROTECT,
        related_name="current_guests",
    )
    source_endpoint = models.ForeignKey(
        ProxmoxEndpoint,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_guests",
    )
    source_scan = models.ForeignKey(
        ScanRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_guests",
    )
    node = models.CharField(max_length=120, db_index=True)
    object_type = models.CharField(max_length=30, choices=ObjectType.choices)
    vmid = models.PositiveIntegerField()
    name = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=80, blank=True)
    cpu_usage = models.FloatField(default=0)
    memory_used_bytes = models.BigIntegerField(default=0)
    memory_max_bytes = models.BigIntegerField(default=0)
    disk_used_bytes = models.BigIntegerField(default=0)
    disk_max_bytes = models.BigIntegerField(default=0)
    uptime_seconds = models.BigIntegerField(default=0)
    runtime_lock = models.CharField(max_length=80, blank=True)
    config = models.JSONField(default=dict, blank=True)
    config_complete = models.BooleanField(default=True)
    disk_references = models.JSONField(default=list, blank=True)
    observed_at = models.DateTimeField(db_index=True)
    runtime_observed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    config_observed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["node", "object_type", "vmid"]
        constraints = [
            # Replaces (object_type, vmid), which wrongly forbade the same VMID in
            # two clusters. nulls_distinct=False keeps the rule enforced for rows not
            # yet backfilled, so a duplicate cannot slip in during the migration.
            models.UniqueConstraint(
                fields=["cluster", "object_type", "vmid"],
                name="unique_current_guest_cluster_identity",
                nulls_distinct=False,
            )
        ]
        indexes = [
            models.Index(fields=["source_endpoint", "object_type"], name="core_curg_endpoint_type_idx"),
            models.Index(fields=["cluster", "node", "object_type", "vmid"], name="core_curg_cluster_node_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.node}: {self.name or self.vmid}"

    def guest_ref(self) -> "GuestRef | None":
        if self.cluster_id is None:
            return None
        return GuestRef(self.cluster.key, self.object_type, self.vmid, self.node)


class CurrentGuestInventoryState(TimestampedModel):
    """Per-cluster projection coverage/freshness.

    Was a singleton (pk=1) describing the whole installation. It is now one record
    per cluster: completeness and absence are evaluated per cluster, and a targeted
    refresh in one cluster must not advance another cluster's freshness. A cluster
    whose every endpoint failed is ``unreachable`` — its guests are unknown, not
    absent, so nothing is retired and its freshness does not advance.
    """

    cluster = models.OneToOneField(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="inventory_state",
    )
    refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_complete_at = models.DateTimeField(null=True, blank=True)
    source_scan = models.ForeignKey(
        ScanRun,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_inventory_states",
    )
    complete = models.BooleanField(default=False)
    # True when no endpoint of the cluster answered: guests shown are last-known,
    # not confirmed absent. Distinct from partial coverage within a reachable cluster.
    unreachable = models.BooleanField(default=False)
    endpoints_attempted = models.JSONField(default=list, blank=True)
    endpoints_succeeded = models.JSONField(default=list, blank=True)
    errors = models.JSONField(default=dict, blank=True)
    # Linked-clone lineage for this cluster as {str(child_vmid): parent_vmid},
    # refreshed by the periodic worker. Passive request rendering reads this
    # instead of issuing a broad live Proxmox lineage read: the cache is per
    # process (LocMem), so a worker-warmed cache never reaches the web process.
    linked_clone_lineage = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cluster"],
                name="unique_inventory_state_per_cluster",
                nulls_distinct=False,
            )
        ]

    def __str__(self) -> str:
        key = self.cluster.key if self.cluster_id else "unqualified"
        return f"Current guest inventory [{key}] ({'complete' if self.complete else 'partial'})"


class ProxmoxStorageConsumer(TimestampedModel):
    """One cluster-qualified node expected to have a storage mounted.

    Gate identity is (storage, cluster, node), consistent with NodeRef. A bare node
    name is not enough: the gate governs destructive file operations, so if two
    clusters each have a `pve1`, an unqualified consumer lets one cluster's scan
    clear the other cluster's gate.
    """

    storage = models.ForeignKey(
        StorageMount,
        on_delete=models.CASCADE,
        related_name="consumer_statuses",
    )
    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.PROTECT,
        related_name="storage_consumers",
    )
    expected_node_name = models.CharField(max_length=120)
    last_successful_inventory_scan = models.DateTimeField(null=True, blank=True)
    last_gate_status = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["storage__display_name", "expected_node_name"]
        constraints = [
            # Replaces the old (storage, expected_node_name) uniqueness, which would
            # wrongly reject cluster B's `pve1` once a second cluster exists.
            # nulls_distinct=False keeps the rule enforced for not-yet-backfilled rows.
            models.UniqueConstraint(
                fields=["storage", "cluster", "expected_node_name"],
                name="unique_storage_cluster_expected_consumer",
                nulls_distinct=False,
            )
        ]

    def node_ref(self) -> "NodeRef | None":
        if self.cluster_id is None:
            return None
        return NodeRef(cluster_key=self.cluster.key, node=self.expected_node_name)

    def __str__(self) -> str:
        cluster_key = self.cluster.key if self.cluster_id is not None else "unqualified"
        return f"{self.storage.storage_id}: {cluster_key}/{self.expected_node_name}"


class ScanClusterObservation(TimestampedModel):
    """One scan's coverage of one cluster.

    A scan stays a global orchestration job, but coverage belongs per cluster: a
    single global list of node names is not adequate historical evidence once nodes
    in different clusters share names.
    """

    scan_run = models.ForeignKey(
        ScanRun,
        on_delete=models.CASCADE,
        related_name="cluster_observations",
    )
    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.PROTECT,
        related_name="scan_observations",
    )
    nodes_attempted = models.JSONField(default=list, blank=True)
    nodes_succeeded = models.JSONField(default=list, blank=True)
    errors = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["cluster__key"]
        constraints = [
            models.UniqueConstraint(
                fields=["scan_run", "cluster"],
                name="unique_scan_cluster_observation",
            )
        ]

    def __str__(self) -> str:
        return f"scan {self.scan_run_id} coverage of {self.cluster.key}"


class ScheduledAction(TimestampedModel):
    class ActionType(models.TextChoices):
        START = "start", "Start"
        SHUTDOWN = "shutdown", "Shutdown"
        STOP = "stop", "Stop"
        REBOOT = "reboot", "Reboot"

    class TargetType(models.TextChoices):
        VM = "vm", "VM"
        CT = "ct", "Container"

    class ScheduleType(models.TextChoices):
        ONCE = "once", "Once"
        RECURRING = "recurring", "Recurring"

    class RecurrenceKind(models.TextChoices):
        ADVANCED = "advanced", "Advanced"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY_ORDINAL = "monthly_ordinal", "Monthly ordinal"
        MONTHLY_DAY = "monthly_day", "Monthly day"

    class CatchUpPolicy(models.TextChoices):
        SKIP_MISSED = "skip_missed", "Skip missed"
        RUN_ONCE_LATE = "run_once_late", "Run once late"

    class LastStatus(models.TextChoices):
        NEVER_RUN = "never_run", "Never run"
        QUEUED = "queued", "Queued"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        MISSED = "missed", "Missed"
        TIMEOUT = "timeout", "Timed out"
        CANCELLED = "cancelled", "Cancelled"

    name = models.CharField(max_length=160)
    enabled = models.BooleanField(default=True)
    action_type = models.CharField(max_length=40, choices=ActionType.choices)
    action_timeout_seconds = models.PositiveIntegerField(default=1800)
    # Additive during the mixed-version rollout. New writers always set it;
    # legacy rows are resolved only through the version-0 sole-cluster adapter.
    cluster = models.ForeignKey(
        ProxmoxCluster,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="scheduled_actions",
    )
    target_type = models.CharField(max_length=20, choices=TargetType.choices)
    target_vmid = models.PositiveIntegerField()
    target_node = models.CharField(max_length=120, blank=True)
    target_name_snapshot = models.CharField(max_length=255, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    schedule_type = models.CharField(max_length=20, choices=ScheduleType.choices, default=ScheduleType.ONCE)
    run_at = models.DateTimeField(null=True, blank=True)
    recurrence = models.JSONField(default=dict, blank=True)
    recurrence_kind = models.CharField(
        max_length=40,
        choices=RecurrenceKind.choices,
        default=RecurrenceKind.ADVANCED,
    )
    timezone = models.CharField(max_length=80, default="UTC")
    catch_up_policy = models.CharField(
        max_length=40,
        choices=CatchUpPolicy.choices,
        default=CatchUpPolicy.SKIP_MISSED,
    )
    max_lateness_minutes = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pve_helper_scheduled_actions",
    )
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_status = models.CharField(
        max_length=40,
        choices=LastStatus.choices,
        default=LastStatus.NEVER_RUN,
    )
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-enabled", "next_run_at", "name"]
        indexes = [
            models.Index(fields=["enabled", "next_run_at"], name="core_sched_enabled_next_idx"),
            models.Index(fields=["target_type", "target_vmid"], name="core_sched_target_idx"),
            models.Index(fields=["cluster", "target_type", "target_vmid"], name="core_sched_cluster_target_idx"),
            models.Index(
                fields=["cluster", "target_node", "target_type", "target_vmid"],
                name="core_sched_cluster_node_idx",
            ),
            models.Index(fields=["action_type"], name="core_sched_action_idx"),
            models.Index(fields=["created_by"], name="core_sched_created_by_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_scheduled_action_name",
            )
        ]

    def __str__(self) -> str:
        target = f"{self.target_type}:{self.target_vmid}"
        return f"{self.name} ({self.action_type} {target})"

    def guest_ref(self) -> "GuestRef | None":
        if self.cluster_id is None:
            return None
        return GuestRef(
            cluster_key=self.cluster.key,
            object_type=self.target_type,
            vmid=self.target_vmid,
            node=self.target_node,
        )


class ScheduledActionRun(TimestampedModel):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PREFLIGHT = "preflight", "Preflight"
        SUBMITTED = "submitted", "Submitted"
        POLLING = "polling", "Polling"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"
        MISSED = "missed", "Missed"
        TIMEOUT = "timeout", "Timed out"
        STALE = "stale", "Stale"
        CANCELLED = "cancelled", "Cancelled"

    class Outcome(models.TextChoices):
        SUCCESS = "success", "Success"
        SUCCESS_NOOP = "success_noop", "Success - no action needed"
        FAILURE = "failure", "Failure"
        SKIPPED = "skipped", "Skipped"
        MISSED = "missed", "Missed"
        TIMEOUT = "timeout", "Timed out"
        STALE = "stale", "Stale"
        CANCELLED = "cancelled", "Cancelled"

    scheduled_action = models.ForeignKey(
        ScheduledAction,
        on_delete=models.PROTECT,
        related_name="runs",
    )
    planned_for = models.DateTimeField()
    occurrence_key = models.CharField(max_length=160)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=40, choices=Status.choices, default=Status.QUEUED)
    outcome = models.CharField(max_length=60, choices=Outcome.choices, blank=True)
    proxmox_task_upid = models.CharField(max_length=512, blank=True)
    proxmox_task_node = models.CharField(max_length=120, blank=True)
    preflight_snapshot = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    triggered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pve_helper_scheduled_action_runs",
    )

    class Meta:
        ordering = ["-planned_for", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["scheduled_action", "occurrence_key"],
                name="uniq_schedaction_occurrence",
            )
        ]
        indexes = [
            models.Index(fields=["scheduled_action", "status"], name="core_schedrun_as_idx"),
            models.Index(fields=["status", "planned_for"], name="core_schedrun_status_plan_idx"),
            models.Index(fields=["proxmox_task_upid"], name="core_schedrun_upid_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.scheduled_action_id}:{self.occurrence_key} ({self.status})"


class TrashItem(TimestampedModel):
    class RestoreStatus(models.TextChoices):
        TRASHED = "trashed", "Trashed"
        RESTORED = "restored", "Restored"
        PURGED = "purged", "Purged"
        FAILED = "failed", "Failed"

    original_path = models.CharField(max_length=1024)
    trash_path = models.CharField(max_length=1024)
    storage_id = models.CharField(max_length=120, blank=True)
    mount = models.ForeignKey(
        StorageMount,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="trash_items",
    )
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
        if self.mount_id and not self.storage_id:
            self.storage_id = self.mount.storage_id
        if not self.storage_id and isinstance(self.metadata, dict):
            self.storage_id = _details_text(self.metadata, "storage_id", 120)
        if not self.mount_id and self.storage_id:
            matches = list(StorageMount.objects.filter(storage_id=self.storage_id)[:2])
            if len(matches) == 1:
                self.mount = matches[0]
        super().save(*args, **kwargs)


class StorageSpaceSnapshot(TimestampedModel):
    # Either a mounted StorageMount (shared/file storages) OR a local API-only
    # storage identified by (node, storage_id). Exactly one of these is set.
    storage = models.ForeignKey(
        StorageMount, on_delete=models.CASCADE, related_name="space_snapshots", null=True, blank=True
    )
    cluster = models.ForeignKey(
        ProxmoxCluster,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="storage_space_snapshots",
    )
    node = models.CharField(max_length=120, blank=True)
    api_storage_id = models.CharField(max_length=120, blank=True)
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
            models.Index(
                fields=["cluster", "node", "api_storage_id", "recorded_at"],
                name="core_space_cl_api_time_idx",
            ),
        ]

    def __str__(self) -> str:
        label = self.storage.storage_id if self.storage_id else f"{self.node}/{self.api_storage_id}"
        return f"{label} @ {self.recorded_at:%Y-%m-%d %H:%M}"
