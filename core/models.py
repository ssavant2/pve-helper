from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone

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


# Columns `AuditEvent` derives from `details` rather than from its own writers,
# with the width each is truncated to. Named once so `save()` and
# `populate_filter_fields_from_details()` cannot drift apart.
DETAIL_DERIVED_AUDIT_FIELDS = {"storage_id": 120, "path": 1024, "target_preallocation": 40}


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
        # The derived columns are what the audit filters and
        # `core_audit_store_path_pre_idx` read, so they have to follow `details`
        # however the row is written. The task modules finish an event with
        # `update_fields=["outcome", "details"]`; without this the recomputation
        # above is discarded on exactly those saves. Same shape as
        # `ProxmoxEndpoint.save()` below.
        if kwargs.get("update_fields") is not None:
            update_fields = set(kwargs["update_fields"])
            if "details" in update_fields:
                kwargs["update_fields"] = sorted(update_fields | set(DETAIL_DERIVED_AUDIT_FIELDS))
        super().save(*args, **kwargs)

    def populate_filter_fields_from_details(self) -> None:
        details = self.details if isinstance(self.details, dict) else {}
        for field, max_length in DETAIL_DERIVED_AUDIT_FIELDS.items():
            setattr(self, field, _details_text(details, field, max_length))

    def __str__(self) -> str:
        return f"{self.timestamp:%Y-%m-%d %H:%M:%S} {self.action} {self.outcome}"


class LogForwarderConfiguration(TimestampedModel):
    """Installation-wide RFC 5424 destination and delivery health."""

    class Transport(models.TextChoices):
        TLS = "tls", "TCP with TLS"
        TCP = "tcp", "TCP"

    enabled = models.BooleanField(default=False)
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(default=6514)
    transport = models.CharField(max_length=12, choices=Transport.choices, default=Transport.TLS)
    facility = models.PositiveSmallIntegerField(default=16)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)

    def __str__(self) -> str:
        return f"log forwarding to {self.host or '-'}:{self.port}"


class LogForwarderTransportTrust(TimestampedModel):
    """Which syslog TLS certificate this installation has agreed to accept.

    `ssl.create_default_context()` reads only the system trust store, which in the
    environment this product targets — a home lab or small firm with an internal CA —
    trusts nothing the operator actually runs. The result was that TLS could not be
    used at all and the only working transport was plaintext TCP, i.e. the whole
    Audit stream in the clear. Trust therefore lives here, decided once by a human
    who looked at the certificate, exactly as `ClusterTransportTrust` does for
    Proxmox endpoints.

    Trust is bound to `host`/`port`: an approval is a statement about one
    destination, so re-pointing the forwarder invalidates it rather than silently
    carrying a decision across to a different collector.
    """

    class Mode(models.TextChoices):
        UNSET = "unset", "Not approved yet"
        # The exact certificate. A renewal is a change the operator must see.
        PINNED = "pinned", "This certificate only"
        # The chain that issued it, trusted exclusively. Renewals from the same CA
        # verify silently; anything else does not.
        CA = "ca", "Any certificate from this issuer"
        # No verification at all. Named for what it is.
        INSECURE = "insecure", "Any certificate (no verification)"

    SINGLETON_PK = 1

    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.UNSET)
    host = models.CharField(max_length=255, blank=True)
    port = models.PositiveIntegerField(default=0)
    # The approved anchor: the leaf for PINNED, the issuing certificate for CA.
    certificate_pem = models.TextField(blank=True)
    sha256_fingerprint = models.CharField(max_length=64, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    issuer = models.CharField(max_length=500, blank=True)
    not_after = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=150, blank=True)

    # What the daily probe last saw on the wire. Kept separate from the approved
    # anchor so "what we agreed to" and "what is being served" stay distinguishable
    # — that difference is the entire point of the change alarm.
    last_checked_at = models.DateTimeField(null=True, blank=True)
    observed_sha256_fingerprint = models.CharField(max_length=64, blank=True)
    observed_subject = models.CharField(max_length=500, blank=True)
    observed_not_after = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"syslog TLS trust for {self.host or '-'}:{self.port} ({self.mode})"


class ManagedCertificate(TimestampedModel):
    """An X.509 certificate this installation stores and serves itself.

    Two usages share one table because they share everything that matters: the
    parsed identity fields, the expiry the daily watch reads, and the deletion rule
    that nothing in use may be removed. Splitting them would duplicate the parser,
    the watch and the panel for a distinction that is one column wide.

    The private key is sealed with `core.services.secret_encryption`, the same
    keyring that seals cluster API tokens, rather than kept as a file: a container
    filesystem is rebuilt on every image update and would lose it, and a key that
    survives only in a bind mount cannot be managed from the UI at all.
    """

    class Usage(models.TextChoices):
        # Presented by nginx for HTTPS. Carries a private key and optional chain.
        SERVER = "server", "HTTPS server certificate"
        # Added to the outbound trust bundle. Public material only.
        AUTHORITY = "authority", "Trusted certificate authority"

    # No index of its own: the unique constraint below already leads with this
    # column, so a second one would be the same b-tree twice.
    usage = models.CharField(max_length=12, choices=Usage.choices)
    label = models.CharField(max_length=150)
    certificate_pem = models.TextField()
    # Intermediates between the leaf and a trusted root, in chain order. Server
    # certificates only; kept apart from the leaf so the identity fields below
    # always describe the certificate the destination actually presents.
    chain_pem = models.TextField(blank=True)
    private_key_sealed = models.TextField(blank=True)

    subject = models.CharField(max_length=500, blank=True)
    issuer = models.CharField(max_length=500, blank=True)
    serial_number = models.CharField(max_length=80, blank=True)
    sha256_fingerprint = models.CharField(max_length=64)
    subject_alt_names = models.JSONField(default=list, blank=True)
    not_before = models.DateTimeField(null=True, blank=True)
    not_after = models.DateTimeField(null=True, blank=True)
    is_certificate_authority = models.BooleanField(default=False)

    source_filename = models.CharField(max_length=255, blank=True)
    uploaded_by = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ["usage", "label"]
        constraints = [
            # The same certificate uploaded twice is one record, not two rows that
            # disagree about which is current. Scoped to usage because a root a CA
            # bundle trusts is a different decision from one served as a leaf.
            models.UniqueConstraint(fields=["usage", "sha256_fingerprint"], name="managed_certificate_unique"),
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.usage})"


class CertificateSettings(TimestampedModel):
    """Which stored certificate terminates HTTPS, and when expiry starts warning.

    `active_certificate` is `PROTECT`ed rather than `SET_NULL`: silently falling
    back to plain HTTP because someone deleted a row is how an installation stops
    being encrypted without anyone deciding that. The UI renders the delete button
    disabled for the certificate in use instead.

    One expiry threshold governs every certificate this installation knows about —
    the syslog destination's, the HTTPS certificate, and each trusted authority.
    Three separately configurable thresholds would be three ways to say the same
    thing and three places to forget.
    """

    SINGLETON_PK = 1

    https_enabled = models.BooleanField(default=False)
    active_certificate = models.ForeignKey(
        ManagedCertificate,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="active_for_settings",
    )
    expiry_warning_enabled = models.BooleanField(default=True)
    expiry_warning_days = models.PositiveSmallIntegerField(default=7)

    def __str__(self) -> str:
        return f"certificates (https={'on' if self.https_enabled else 'off'})"


class ScheduledActionSettings(TimestampedModel):
    """Installation-wide settings for scheduled task execution history."""

    SINGLETON_PK = 1

    run_history_retention_days = models.PositiveSmallIntegerField(default=90)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    run_history_retention_days__gte=1,
                    run_history_retention_days__lte=999,
                ),
                name="scheduled_action_retention_days_range",
            ),
        ]

    def __str__(self) -> str:
        return f"scheduled tasks ({self.run_history_retention_days} days retention)"


class LogForwardingDelivery(models.Model):
    """A durable, destination-independent snapshot awaiting syslog delivery."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SENDING = "sending", "Sending"
        SENT = "sent", "Sent"

    audit_event_id = models.PositiveBigIntegerField(db_index=False)
    sequence = models.PositiveIntegerField(default=1)
    payload = models.JSONField(default=dict)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["audit_event_id", "sequence"], name="uniq_log_delivery_event_sequence")
        ]

    def __str__(self) -> str:
        return f"audit:{self.audit_event_id}#{self.sequence} ({self.status})"


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

    class RetirementMode(models.TextChoices):
        VERIFIED = "verified", "Verified"
        FORCED = "forced", "Forced"

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

    # --- Retirement lifecycle (see docs/cluster-retire.local.md). ---
    # `retired_at` is the single source of truth for lifecycle state. A retired
    # cluster is always disabled and carries a mode; it is never re-enabled and is
    # excluded from every managed/provider-acquirable scope. `enabled=False` alone
    # stays reversible and never means retired.
    retired_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    retirement_mode = models.CharField(max_length=16, choices=RetirementMode.choices, blank=True)
    retirement_reason = models.CharField(max_length=1000, blank=True)
    # Tombstone copies of the pinned identity, taken as the live pinned columns are
    # cleared on retirement. Non-unique on purpose: the physical cluster is released
    # so it can be re-onboarded under a new key, while these preserve what the
    # retired row once described for Audit and the read-only detail page.
    retired_ca_uuid = models.CharField(max_length=64, blank=True)
    retired_ca_fingerprint = models.CharField(max_length=200, blank=True)
    # Retirement bumps this. The signed retirement preflight binds it so a token
    # issued against one lifecycle state is rejected after any concurrent change.
    lifecycle_generation = models.PositiveBigIntegerField(default=1)
    # Monotonic, never-cleared memory that this cluster once carried operational
    # footprint. Stamped the first time it acquires any non-configuration footprint
    # and never reset by a retention purge, cleanup or retirement, so hard-delete
    # eligibility can never be recovered by waiting for timed retention to run.
    operational_footprint_at = models.DateTimeField(null=True, blank=True)
    operational_footprint_reason = models.CharField(max_length=64, blank=True)

    # Per-cluster node-enrollment activation. Version is the irreversible 0→1
    # feature boundary; generation is the independent enrollment-set clock.
    # 5a1H owns every mutation and advances generation under the locked row.
    enrollment_contract_version = models.PositiveSmallIntegerField(default=0)
    enrollment_generation = models.PositiveBigIntegerField(default=0)
    enrollment_activated_at = models.DateTimeField(null=True, blank=True)
    enrollment_activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["key"]
        constraints = [
            models.UniqueConstraint(
                Lower("key"),
                name="unique_cluster_key_case_insensitive",
            ),
            # Released, not reserved: a retired tombstone must not keep a physical
            # cluster's CA UUID claimed, or that hardware could never be onboarded
            # again. Narrowed to live rows so uniqueness holds only among managed
            # clusters; retirement copies the value into the tombstone columns.
            models.UniqueConstraint(
                fields=["discovered_ca_uuid"],
                condition=~models.Q(discovered_ca_uuid="") & models.Q(retired_at__isnull=True),
                name="unique_nonblank_cluster_ca_uuid",
            ),
            # A retired cluster is disabled and has a mode; both parts hold together
            # or the row is rejected. Forced retirement stamps enabled=False and
            # retired_at in one transaction precisely so this can never be violated.
            models.CheckConstraint(
                name="retired_cluster_is_disabled_and_moded",
                condition=(
                    models.Q(retired_at__isnull=True) | (models.Q(enabled=False) & ~models.Q(retirement_mode=""))
                ),
            ),
            # An active (non-retired) cluster carries no retirement metadata, so a
            # mode/reason/actor can never linger on a row that was never retired.
            models.CheckConstraint(
                name="active_cluster_has_no_retirement_metadata",
                condition=(
                    models.Q(retired_at__isnull=False)
                    | (
                        models.Q(retirement_mode="")
                        & models.Q(retirement_reason="")
                        & models.Q(retired_by__isnull=True)
                    )
                ),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.key})"

    @property
    def is_retired(self) -> bool:
        """Whether this cluster has been retired. ``retired_at`` is the single
        source of truth; ``enabled=False`` alone is reversible and not retirement."""
        return self.retired_at is not None


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
        # Additive: the public store *and* this cluster's own CA. Proxmox's own
        # model — a cluster whose nodes are split between a publicly trusted
        # pveproxy certificate and the default internal one is otherwise
        # inexpressible, since CA_PEM is exclusive by design.
        PUBLIC_PLUS_CA = "public_ca_pem", "Public CA store plus cluster CA"

    cluster = models.OneToOneField(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="transport_trust",
    )
    mode = models.CharField(max_length=20, choices=Mode.choices, default=Mode.PUBLIC)
    # The CA bundle for both bundle-bearing modes — trusted exclusively under
    # CA_PEM, alongside the public store under PUBLIC_PLUS_CA. Empty for PUBLIC.
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
        # Covered by `unique_endpoint_name_per_cluster`; see `FileInventory.storage`.
        db_index=False,
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
        # Covered by `core_con_cluster_target_idx`.
        db_index=False,
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
    # Not `db_index=True`: `core_console_status_exp_idx` starts with this column.
    # The implicit index also brought a `varchar_pattern_ops` twin for prefix
    # matching, which nothing wants — `status` is an enum, compared with `=`.
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.PENDING)
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

    def guest_ref(self) -> GuestRef | None:
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

    class IdentitySource(models.TextChoices):
        DERIVED = "derived", "Derived from the Proxmox definition"
        MANUAL = "manual", "Entered by an operator"

    # Whether backend_identity was composed from ClusterStorage.config or typed
    # by hand. A hand-typed identity is the one that can silently disagree with
    # another cluster's spelling of the same export.
    identity_source = models.CharField(
        max_length=16,
        choices=IdentitySource.choices,
        default=IdentitySource.MANUAL,
    )
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
        # Covered by `core_cstorage_state_idx` and two more.
        db_index=False,
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
    # Distinct from ``retired_at`` above: catalog refresh sets ``retired_at`` when
    # Proxmox no longer publishes the definition, while ``unmanaged_at`` means
    # pve-helper deliberately stopped managing the owning cluster. Retirement
    # keeps this definition as a durable tombstone but every current catalog
    # reader excludes it.
    unmanaged_at = models.DateTimeField(null=True, blank=True)
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
            models.Index(fields=["cluster", "unmanaged_at"], name="core_cstorage_unmanaged_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.cluster.key}/{self.storage_id}"


class ClusterStorageNodeState(TimestampedModel):
    """Current state of one node's access to one cluster storage definition."""

    cluster_storage = models.ForeignKey(
        ClusterStorage,
        on_delete=models.CASCADE,
        related_name="node_states",
        # Covered by `core_csnode_state_idx`. This was the busiest of the
        # redundant indexes by scan count; the composite serves the same lookups.
        db_index=False,
    )
    node = models.CharField(max_length=120)
    active = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    total_bytes = models.BigIntegerField(null=True, blank=True)
    used_bytes = models.BigIntegerField(null=True, blank=True)
    available_bytes = models.BigIntegerField(null=True, blank=True)
    present = models.BooleanField(default=True)
    # Whether the node itself failed to answer, as opposed to answering that the
    # storage is not there. Both leave `present` False, because absence still
    # requires proof and every gate that reads `present` must keep refusing. The
    # distinction is for the operator: a node taken down for patching must stay
    # visible and be labelled unknown, not silently vanish from the tree as if
    # its disks had been removed.
    unreachable = models.BooleanField(default=False)
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
        # Covered by `unique_cluster_storage_mount_scope`.
        db_index=False,
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


class ClusterStorageVolumeCoverage(TimestampedModel):
    """Publication state for one independently knowable storage scope."""

    class Scope(models.TextChoices):
        SHARED = "shared", "Shared"
        NODE = "node", "Node"

    cluster_storage = models.ForeignKey(
        ClusterStorage,
        on_delete=models.CASCADE,
        related_name="volume_coverages",
        # Covered by `core_csvolcov_state_idx`.
        db_index=False,
    )
    scope = models.CharField(max_length=12, choices=Scope.choices)
    node = models.CharField(max_length=120, null=True, blank=True)
    volume_generation = models.UUIDField(null=True, blank=True)
    based_on_metadata_generation = models.UUIDField(null=True, blank=True)
    refreshed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    complete = models.BooleanField(default=False)
    error_code = models.CharField(max_length=64, blank=True)
    error_reason = models.CharField(max_length=255, blank=True)
    # Which nodes answered identically for a shared definition. The agreement is
    # the proof, so it is recorded once per scope rather than by storing one
    # duplicate copy of the whole volume list per answering node.
    agreeing_nodes = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["cluster_storage__cluster__key", "cluster_storage__storage_id", "node"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(scope="shared", node__isnull=True) | models.Q(scope="node", node__isnull=False)),
                name="storage_volume_coverage_scope_node",
            ),
            models.UniqueConstraint(
                fields=["cluster_storage", "node"],
                name="unique_storage_volume_coverage_scope",
                nulls_distinct=False,
            ),
        ]
        indexes = [
            models.Index(
                fields=["cluster_storage", "complete"],
                name="core_csvolcov_state_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.cluster_storage}@{self.node or 'shared'} coverage"


class ClusterStorageVolumeObservation(TimestampedModel):
    """Mutable current content observation, qualified by the answering node."""

    cluster_storage = models.ForeignKey(
        ClusterStorage,
        on_delete=models.CASCADE,
        related_name="volume_observations",
        # Covered by `core_csvol_generation_idx`.
        db_index=False,
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
            # Column order is deliberate: the tuple is what must be unique, but
            # the backing index also has to serve the classification hot path
            # (cluster_storage + volid) and the refresh diff, which match on the
            # same prefix. Reordering costs nothing and avoids a fourth index on
            # the most write-heavy table in the app.
            models.UniqueConstraint(
                fields=["cluster_storage", "volid", "node"],
                name="unique_cluster_storage_volume_observation",
            )
        ]
        # The comment above applies here too, and once had to be read twice:
        # single-column indexes on `vmid` and `content` were added three lines
        # below the reasoning that avoided a fourth index. Neither could be
        # reached — no query filters `vmid` at all, and the one that filters
        # `content` binds `cluster_storage`, `observed_volume_generation` and
        # `node` in the same call, so the composite below wins outright.
        indexes = [
            models.Index(
                fields=["cluster_storage", "observed_volume_generation"],
                name="core_csvol_generation_idx",
            ),
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

    # `db_index=False` on both FKs below, and on fifteen more across this module,
    # because a wider index on the same model already starts with the same column.
    # Django adds an index to every ForeignKey by default; when that column also
    # leads a composite or a unique constraint, the implicit one is a strict
    # prefix — a second btree maintained on every write to answer what the wider
    # one already answers, including the cascade check. Postgres will happily use
    # the narrower index when both exist (it is smaller), so scan counts look busy
    # right up until you remove it and the lookups move to the composite at the
    # same complexity. `InventoryIndexInvariantTests` enforces the rule model-wide.
    # This table is where it matters most: one row per file per scan.
    scan_run = models.ForeignKey(ScanRun, on_delete=models.CASCADE, related_name="files", db_index=False)
    storage = models.ForeignKey(StorageMount, on_delete=models.CASCADE, related_name="files", db_index=False)
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
            # `classification` and `content_category` are single-column on
            # purpose: both hot queries constrain the scan and the storage with
            # an OR of `Q(scan_run, storage)` pairs — one per storage on screen
            # — which the unique constraint's prefix serves per branch, leaving
            # these two to be bitmap-ANDed against the union. A composite led
            # by `scan_run` would only duplicate that prefix.
            # There is deliberately no index on `derived_volid`: it is written
            # and displayed, never filtered. Admin's `search_fields` does an
            # `ILIKE`, which no btree index here would answer.
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

    # Covered by `core_proxmo_scan_ru_7d6c24_idx`; see `FileInventory.scan_run`.
    scan_run = models.ForeignKey(ScanRun, on_delete=models.CASCADE, related_name="proxmox_objects", db_index=False)
    # Which cluster this scan evidence came from. New rows are always qualified.
    # Nullable only to retain genuinely ambiguous pre-contract history; such rows
    # are display evidence and fail closed if considered by a file action.
    cluster = models.ForeignKey(
        ProxmoxCluster,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="proxmox_objects",
        # Covered by `core_pinv_cluster_type_vmid`.
        db_index=False,
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

    def guest_ref(self) -> GuestRef | None:
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
        # Covered by `core_curg_cluster_node_idx`.
        db_index=False,
    )
    source_endpoint = models.ForeignKey(
        ProxmoxEndpoint,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_guests",
        # Covered by `core_curg_endpoint_type_idx`.
        db_index=False,
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
    # Cluster-wide runtime facts from cluster/resources, refreshed on every live
    # reconcile: the guest's resource pool and HA state (blank = not HA managed).
    pool = models.CharField(max_length=255, blank=True, default="")
    ha_state = models.CharField(max_length=40, blank=True, default="")
    # Guest-agent enrichment (OS pretty name, hostname, IPs) fetched by the periodic
    # worker so overview/summary read one shared, cross-process copy instead of each
    # web process fanning out its own agent calls. Empty until the agent answers.
    agent_info = models.JSONField(default=dict, blank=True)
    agent_observed_at = models.DateTimeField(null=True, blank=True)
    config = models.JSONField(default=dict, blank=True)
    config_complete = models.BooleanField(default=True)
    disk_references = models.JSONField(default=list, blank=True)
    observed_at = models.DateTimeField(db_index=True)
    runtime_observed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    config_observed_at = models.DateTimeField(null=True, blank=True)
    # Whether this row may be shown as ordinary inventory. False means the operator
    # has not enrolled the node it sits on as `managed`. The row is deliberately kept
    # rather than deleted: the storage risk gate and volume-usage classification read
    # this table as safety evidence, and a hidden node's live disk must still block a
    # destructive file action. `core.services.publication_scope` is the only writer of
    # this pair; read seams in `current_guest_inventory` are the only readers.
    published = models.BooleanField(default=True)
    based_on_enrollment_generation = models.PositiveBigIntegerField(default=0)

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

    def guest_ref(self) -> GuestRef | None:
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
    # The node names this cluster was completely read from during the pass that
    # produced this row. Endpoint coverage is not node coverage: the scan's pass-2
    # gap fill reads nodes that have no endpoint row of their own, so a node can be
    # attempted and fail without any endpoint failing. Retirement is decided against
    # this set, never against `complete` alone.
    covered_nodes = models.JSONField(default=list, blank=True)
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
        # Covered by `unique_storage_cluster_expected_consumer`.
        db_index=False,
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

    def node_ref(self) -> NodeRef | None:
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
        # Covered by `unique_scan_cluster_observation`.
        db_index=False,
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
        # The sentinel for `ScheduleType.ONCE`, which has no recurrence at all. It is
        # not offered in the form and `next_run_after()` never reaches it: a one-time
        # schedule returns early on its `run_at`. Until Review 11 this role was played
        # by an `ADVANCED` member that also claimed to mean "operator-supplied RRULE",
        # a second meaning nothing implemented soundly.
        NONE = "none", "Not recurring"
        DAILY = "daily", "Daily"
        WEEKLY = "weekly", "Weekly"
        MONTHLY_ORDINAL = "monthly_ordinal", "Monthly ordinal"
        MONTHLY_DAY = "monthly_day", "Monthly day"

    class CatchUpPolicy(models.TextChoices):
        SKIP_MISSED = "skip_missed", "Skip missed"
        RUN_ONCE_LATE = "run_once_late", "Run once late"

    class EndCondition(models.TextChoices):
        NONE = "none", "No end"
        RUN_UNTIL = "run_until", "Run until"
        RUN_COUNT = "run_count", "Run a fixed number of times"

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
    # Mandatory since `0027`: the nav tree groups schedules under their cluster,
    # and a null has no honest place in that tree. The rollout's legacy rows were
    # backfilled there, which is also where the sole-cluster adapter stopped being
    # the only thing resolving them.
    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.PROTECT,
        related_name="scheduled_actions",
        # Covered by `core_sched_cluster_target_idx`.
        db_index=False,
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
        default=RecurrenceKind.NONE,
    )
    timezone = models.CharField(max_length=80, default="UTC")
    catch_up_policy = models.CharField(
        max_length=40,
        choices=CatchUpPolicy.choices,
        default=CatchUpPolicy.SKIP_MISSED,
    )
    max_lateness_minutes = models.PositiveIntegerField(default=0)
    end_condition = models.CharField(
        max_length=20,
        choices=EndCondition.choices,
        default=EndCondition.NONE,
    )
    run_until = models.DateTimeField(null=True, blank=True)
    max_scheduled_runs = models.PositiveSmallIntegerField(null=True, blank=True)
    scheduled_run_count = models.PositiveIntegerField(default=0)
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
            # Per cluster, not fleet-wide: the nav tree presents schedules as
            # belonging to a cluster, so refusing "Nightly backup" in the second
            # cluster because the first one has it reads as a bug. Names are the
            # operator's own labels and clusters are independent installations.
            models.UniqueConstraint(
                fields=["cluster", "name"],
                condition=models.Q(deleted_at__isnull=True),
                name="uniq_active_scheduled_action_name_per_cluster",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(end_condition="none", run_until__isnull=True, max_scheduled_runs__isnull=True)
                    | models.Q(end_condition="run_until", run_until__isnull=False, max_scheduled_runs__isnull=True)
                    | models.Q(
                        end_condition="run_count",
                        run_until__isnull=True,
                        max_scheduled_runs__gte=1,
                        max_scheduled_runs__lte=999,
                    )
                ),
                name="scheduled_action_end_condition_fields",
            ),
        ]

    def __str__(self) -> str:
        target = f"{self.target_type}:{self.target_vmid}"
        return f"{self.name} ({self.action_type} {target})"

    def guest_ref(self) -> GuestRef | None:
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
        # Covered by `core_schedrun_as_idx`.
        db_index=False,
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

    # `mount` and `storage_id` are creation inputs, not derived state: whoever
    # trashes a file knows which storage it came from. `save()` used to backfill
    # them from `metadata` or from a storage_id lookup, which no live writer had
    # needed since both creation paths in `services/storage_actions.py` started
    # passing them — and the lookup gave up silently when a storage_id matched two
    # mounts, so it read as a guarantee it could not make.
    # `TrashItemCreationContractTests` holds the creation paths to it instead.


class StorageSpaceSnapshot(TimestampedModel):
    # Either a mounted StorageMount (shared/file storages) OR a local API-only
    # storage identified by (node, storage_id). Exactly one of these is set, and
    # `storage_space_snapshot_scope` is what makes that a fact rather than a
    # comment — see the constraint for why the API branch also requires a cluster.
    # Covered by `core_storag_storage_10c3c9_idx`.
    storage = models.ForeignKey(
        StorageMount,
        on_delete=models.CASCADE,
        related_name="space_snapshots",
        null=True,
        blank=True,
        db_index=False,
    )
    cluster = models.ForeignKey(
        ProxmoxCluster,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="storage_space_snapshots",
        # Covered by `core_space_cl_api_time_idx`.
        db_index=False,
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
        constraints = [
            # A snapshot is only readable through the branch it was written for.
            # `_storage_space_chart_data` selects on `storage`;
            # `_api_storage_space_chart_data` selects on
            # `storage__isnull=True, cluster=..., api_storage_id=...`. So a row
            # that sets neither identity, or sets both, or is API-side without a
            # cluster, is not a mildly malformed sample — it is a sample no chart
            # can ever reach. Eight such rows existed when this landed, written
            # before `0015` added `cluster`, and nothing had noticed.
            #
            # Both halves are constrained, not just the identity XOR, for the same
            # reason `ClusterStorageMount` and `ClusterStorageVolumeCoverage`
            # constrain scope against node: the reader's filter is the real
            # contract, and it reads more than one column. `node` and
            # `api_storage_id` are `blank=True` rather than nullable, so the empty
            # string is the absent value here.
            models.CheckConstraint(
                condition=(
                    models.Q(storage__isnull=False, cluster__isnull=True, node="", api_storage_id="")
                    | (
                        models.Q(storage__isnull=True, cluster__isnull=False)
                        & ~models.Q(node="")
                        & ~models.Q(api_storage_id="")
                    )
                ),
                name="storage_space_snapshot_scope",
            ),
        ]
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


class ClusterMembershipState(TimestampedModel):
    """One cluster's current membership and topology role. Module 5 phase 5a1A.

    Cluster-grain: node-grain facts live in :class:`ClusterNodeState`. Published
    transactionally by the 5a1B reconciler; web processes only read it.

    The registered topology role deliberately remains free text at rest. A value
    written by a newer build reads as ``UNKNOWN`` and is re-adopted only from a
    later complete observation; it never guesses a Hosts/Clusters group.

    The pending-transition columns land with 5a1G's confirmation surface. The
    boolean is the fail-closed acquisition gate; the free-text target preserves
    forward compatibility without silently dropping a block written by a newer
    build. An unreadable target has an explicit Connections repair ceremony.
    """

    cluster = models.OneToOneField(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="membership_state",
    )
    #: Free text on purpose: a value this build does not recognize must be
    #: readable and re-adoptable, not a database error in a periodic reconciler.
    topology_role = models.CharField(max_length=32, default="unknown")
    transition_pending = models.BooleanField(default=False)
    pending_topology_role = models.CharField(max_length=32, default="unknown")
    membership_generation = models.PositiveBigIntegerField(default=0)
    member_count = models.PositiveIntegerField(default=0)
    quorate = models.BooleanField(default=False)
    #: The node that answered the read, from `cluster/status`'s `local=1` row.
    #: Provenance, not identity: a read from a node this scope does not accept as
    #: a member is not evidence about this scope.
    observed_from = models.CharField(max_length=120, blank=True, default="")

    class Meta:
        verbose_name = "cluster membership state"
        verbose_name_plural = "cluster membership states"
        constraints = [
            models.CheckConstraint(
                name="core_membership_pending_role_coherent",
                condition=(
                    models.Q(transition_pending=False, pending_topology_role="unknown")
                    | (
                        models.Q(transition_pending=True)
                        & ~models.Q(pending_topology_role__in=("", "unknown"))
                        & ~models.Q(pending_topology_role=models.F("topology_role"))
                    )
                ),
            )
        ]

    def __str__(self) -> str:
        return f"{self.cluster.key}: {self.topology_role}"

    def role(self):
        """Return :attr:`topology_role` as a `TopologyRole`, or ``UNKNOWN``."""
        from core.services.cluster_topology_role import TopologyRole

        try:
            return TopologyRole(self.topology_role)
        except ValueError:
            return TopologyRole.UNKNOWN

    def pending_role(self):
        """Return the pending target as a typed role, or ``UNKNOWN``.

        ``transition_pending`` remains authoritative when this returns UNKNOWN:
        that is a newer-build/hand-edited value and must stay blocked until the
        explicit repair action discards the unreadable evidence.
        """
        from core.services.cluster_topology_role import TopologyRole

        try:
            return TopologyRole(self.pending_topology_role)
        except ValueError:
            return TopologyRole.UNKNOWN

    @property
    def pending_role_is_readable(self) -> bool:
        from core.services.cluster_topology_role import TopologyRole

        return self.pending_topology_role in {member.value for member in TopologyRole}

    @property
    def role_is_readable(self) -> bool:
        """False when the stored role came from a build this one cannot read.

        Surfaced rather than swallowed: adopting over it is correct, but it is a
        Hosts/Clusters group change nobody asked for and the operator should see
        that it happened.
        """
        from core.services.cluster_topology_role import TopologyRole

        return self.topology_role in {member.value for member in TopologyRole}


class ClusterTopologyHandoffStorageBinding(TimestampedModel):
    """One operator-confirmed storage mapping waiting on replacement coverage.

    The old definition is never reassigned. The hand-off snapshots the exact
    storage ID, scope, node and mount onto the replacement identity. Only that
    replacement's own complete metadata generation may apply the intent.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPLIED = "applied", "Applied"
        REFUSED = "refused", "Refused"

    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="topology_handoff_storage_bindings",
        # Covered by ``core_topology_handoff_binding_uniq``.
        db_index=False,
    )
    source_cluster_key_snapshot = models.CharField(max_length=63)
    storage_id = models.CharField(max_length=120)
    mount = models.ForeignKey(
        StorageMount,
        on_delete=models.PROTECT,
        related_name="topology_handoff_intents",
    )
    scope = models.CharField(max_length=12, choices=ClusterStorageMount.Scope.choices)
    node = models.CharField(max_length=120, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    applied_at = models.DateTimeField(null=True, blank=True)
    refusal_reason = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["cluster__key", "storage_id", "node"]
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "storage_id", "node"],
                name="core_topology_handoff_binding_uniq",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                name="core_topology_handoff_binding_scope",
                condition=(
                    models.Q(scope=ClusterStorageMount.Scope.SHARED, node__isnull=True)
                    | (models.Q(scope=ClusterStorageMount.Scope.NODE, node__isnull=False) & ~models.Q(node=""))
                ),
            ),
            models.CheckConstraint(
                name="core_topology_handoff_binding_status",
                condition=models.Q(status__in=("pending", "applied", "refused")),
            ),
            models.CheckConstraint(
                name="core_topology_handoff_source_nonempty",
                condition=~models.Q(source_cluster_key_snapshot=""),
            ),
        ]


class ClusterNodeState(TimestampedModel):
    """One `NodeRef`'s typed membership/runtime projection. Module 5 phase 5a1A.

    This is the discovery projection `docs/node-enrollment.local.md` requires and
    the row 5a1C publishes node-runtime facts into. Membership and runtime keep
    separate generation fields and coverage rows, so one targeted runtime update
    cannot make older membership evidence look fresh (or vice versa). Missing
    runtime metrics stay ``NULL``/blank; an absent provider key is unknown, never
    a healthy-looking zero.

    Rows are a **current projection, not history**. A node absent from a complete
    generation becomes ``present=False`` rather than being deleted, because
    Connections must tell "disappeared" apart from "never enrolled".
    """

    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="node_states",
        # Covered by both `core_cluster_node_state_uniq` and
        # `core_node_state_present_idx`, which lead with this column. A separate
        # single-column index would be a strict prefix of both.
        db_index=False,
    )
    node_name = models.CharField(max_length=120)
    #: Corosync node id. **Evidence only, never identity** -- it is reassignable.
    nodeid = models.PositiveIntegerField(null=True, blank=True)
    #: False only under a complete membership generation. A failed read leaves
    #: the previous value untouched.
    present = models.BooleanField(default=True)
    online = models.BooleanField(default=False)
    #: The corosync ring address from `cluster/status`'s `ip`. **A suggestion for
    #: prefilling an Add-node form, never proven reachability**: it may be a
    #: cluster-internal network, and it carries no port or scheme.
    reported_ring_address = models.CharField(max_length=255, blank=True, default="")
    membership_generation = models.PositiveBigIntegerField(default=0)
    first_discovered_at = models.DateTimeField(null=True, blank=True)
    last_discovered_at = models.DateTimeField(null=True, blank=True)

    # Node-runtime domain. 5a1C normalizes `nodes/<node>/status` into these
    # decision/display fields; raw provider payloads are not authoritative state.
    runtime_generation = models.PositiveBigIntegerField(default=0)
    cpu_usage = models.FloatField(null=True, blank=True)
    cpu_wait = models.FloatField(null=True, blank=True)
    cpu_model = models.CharField(max_length=255, blank=True, default="")
    cpu_sockets = models.PositiveSmallIntegerField(null=True, blank=True)
    cpu_cores = models.PositiveSmallIntegerField(null=True, blank=True)
    memory_total_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    memory_used_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    swap_total_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    swap_used_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    rootfs_total_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    rootfs_used_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    load_average_1m = models.FloatField(null=True, blank=True)
    load_average_5m = models.FloatField(null=True, blank=True)
    load_average_15m = models.FloatField(null=True, blank=True)
    uptime_seconds = models.PositiveBigIntegerField(null=True, blank=True)
    pve_version = models.CharField(max_length=120, blank=True, default="")
    kernel_version = models.CharField(max_length=255, blank=True, default="")
    current_kernel_release = models.CharField(max_length=255, blank=True, default="")
    boot_mode = models.CharField(max_length=32, blank=True, default="")
    secure_boot_enabled = models.BooleanField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["cluster", "node_name"], name="core_cluster_node_state_uniq"),
            models.CheckConstraint(
                condition=~models.Q(node_name="") & ~models.Q(node_name__contains=":"),
                name="core_cluster_node_state_valid_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["cluster", "present"], name="core_node_state_present_idx"),
        ]
        verbose_name = "cluster node state"
        verbose_name_plural = "cluster node states"

    def __str__(self) -> str:
        return f"{self.cluster.key}/{self.node_name}"


class ClusterProjectionCoverage(TimestampedModel):
    """What one refresh of one scope actually proved. Module 5 phase 5a1A.

    Keyed by ``(cluster, domain, node_name)`` where a null node means the scope is
    cluster-grain. **The uniqueness is `NULLS NOT DISTINCT`**: without it
    PostgreSQL treats every cluster-grain row as distinct from every other, so a
    domain would silently accumulate one coverage row per refresh and "the
    current coverage" would stop being a single answerable question.

    ``complete`` is the only authority for absence. ``based_on_generation``
    records which membership generation a composed scope was derived from, so a
    node-grain refresh cannot be read as current against a newer membership.
    """

    DOMAIN_MEMBERSHIP = "membership"
    DOMAIN_NODE_RUNTIME = "node_runtime"
    DOMAIN_NODE_NETWORK = "node_network"

    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="projection_coverage",
        # Covered by `core_projection_coverage_uniq`, which leads with it.
        db_index=False,
    )
    domain = models.CharField(
        max_length=64,
        choices=(
            (DOMAIN_MEMBERSHIP, "Membership"),
            (DOMAIN_NODE_RUNTIME, "Node runtime"),
            (DOMAIN_NODE_NETWORK, "Node network"),
        ),
    )
    #: Null for a cluster-grain scope. Part of the identity, see the class note.
    node_name = models.CharField(max_length=120, null=True, blank=True)
    generation = models.PositiveBigIntegerField(default=0)
    #: The membership generation this scope was composed against, when composed.
    based_on_generation = models.PositiveBigIntegerField(null=True, blank=True)
    complete = models.BooleanField(default=False)
    attempted_at = models.DateTimeField(null=True, blank=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    #: A stable domain code, never a provider or Python exception string. Raw
    #: detail belongs in protected logs.
    error_code = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "domain", "node_name"],
                name="core_projection_coverage_uniq",
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        domain="membership",
                        node_name__isnull=True,
                        based_on_generation__isnull=True,
                    )
                    | (
                        # Node-grained domains share one arm. They impose the same
                        # rule, and a per-domain arm would be a third place to keep
                        # in step with `core_cluster_node_state_valid_ref`.
                        models.Q(
                            domain__in=("node_runtime", "node_network"),
                            node_name__isnull=False,
                            based_on_generation__isnull=False,
                        )
                        & ~models.Q(node_name="")
                        & ~models.Q(node_name__contains=":")
                    )
                ),
                name="core_projection_coverage_scope",
            ),
        ]
        verbose_name = "cluster projection coverage"
        verbose_name_plural = "cluster projection coverage"

    def __str__(self) -> str:
        scope = f"{self.domain}/{self.node_name}" if self.node_name else self.domain
        return f"{self.cluster.key}: {scope}"


class ClusterNodeInterface(TimestampedModel):
    """One network interface on one node, as the provider last described it. 5a4B-i.

    The row exists to answer **what a guest NIC may attach to on this node**, without
    a provider call on render. Two consumers computed that answer independently and
    both got it wrong against live production, in opposite directions; the projection
    is where that answer stops being re-derived.

    ``attachable`` is published, never derived. It is true iff
    ``nodes/<node>/network?type=any_bridge`` returned this interface for this node.
    Type, name and presence in the plain interface listing are all insufficient: a
    realized SDN vnet with no address is absent from the plain listing entirely, one
    with an address comes back ``unknown``, and the cluster-wide vnet list carries no
    node opinion at all -- which is how one consumer came to offer a vnet on a node
    whose zone excludes it.

    **Two flags, because absence and ignorance are different answers.**
    ``present=False, unreachable=False`` means a complete read proved the interface
    is gone. ``unreachable=True`` means the node did not answer and the row is
    unknown -- a node taken down for patching must read as unknown, never as "no
    bridges", because a consumer that reads the latter as proof disables a legitimate
    migration target. This mirrors :class:`ClusterStorageNodeState`, which carries the
    same pair for the same reason.

    Rows are tombstoned, never deleted, so "this bridge disappeared" stays
    distinguishable from "this node was never swept".
    """

    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="node_interfaces",
        # Covered by `core_node_interface_uniq`, which leads with this column.
        db_index=False,
    )
    #: Deliberately **not** an FK to `ClusterNodeState`: a cascade would delete the
    #: tombstones this model exists to keep. The valid-ref check below is the same
    #: one `core_cluster_node_state_valid_ref` applies.
    node_name = models.CharField(max_length=120)
    iface = models.CharField(max_length=120)

    #: From the `any_bridge` answer wherever the two reads overlap, so a realized
    #: vnet is never stored as `unknown`. Live values: bridge, bond, eth, vnet,
    #: unknown. No `OVSBridge` was observed on any probed node.
    interface_type = models.CharField(max_length=32, blank=True, default="")
    #: Published, never derived. See the class note.
    attachable = models.BooleanField(default=False)
    active = models.BooleanField(null=True, blank=True)
    autostart = models.BooleanField(null=True, blank=True)
    method = models.CharField(max_length=32, blank=True, default="")
    address = models.CharField(max_length=64, blank=True, default="")
    cidr = models.CharField(max_length=64, blank=True, default="")
    gateway = models.CharField(max_length=64, blank=True, default="")
    bridge_ports = models.CharField(max_length=255, blank=True, default="")
    bridge_vids = models.CharField(max_length=255, blank=True, default="")
    bridge_vlan_aware = models.BooleanField(null=True, blank=True)
    bond_mode = models.CharField(max_length=64, blank=True, default="")
    bond_slaves = models.CharField(max_length=255, blank=True, default="")
    #: Opaque presentation text. 5a4B stores no SDN concept: no zone, no tag, no
    #: controller. Those are 5a4C's.
    comments = models.TextField(blank=True, default="")

    #: Advanced only by a pass in which **both** per-node reads succeeded. Currency
    #: is equality with this node's `node_network` coverage generation, never age.
    observed_generation = models.PositiveBigIntegerField(default=0)
    #: The enrollment generation the publishing pass was composed against, so a row
    #: published before a node was hidden is detectable rather than merely stale.
    based_on_enrollment_generation = models.PositiveBigIntegerField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    present = models.BooleanField(default=True)
    #: The node failed to answer, as opposed to answering that the interface is
    #: gone. Both leave `present` False; only this one means "unknown".
    unreachable = models.BooleanField(default=False)

    class Meta:
        ordering = ["cluster__key", "node_name", "iface"]
        constraints = [
            models.UniqueConstraint(fields=["cluster", "node_name", "iface"], name="core_node_interface_uniq"),
            models.CheckConstraint(
                condition=~models.Q(node_name="") & ~models.Q(node_name__contains=":") & ~models.Q(iface=""),
                name="core_node_interface_valid_ref",
            ),
        ]
        indexes = [
            models.Index(fields=["cluster", "node_name", "attachable"], name="core_node_iface_attach_idx"),
        ]
        verbose_name = "cluster node interface"
        verbose_name_plural = "cluster node interfaces"

    def __str__(self) -> str:
        return f"{self.cluster.key}/{self.node_name}/{self.iface}"


class ClusterNodeEnrollment(TimestampedModel):
    """Operator-owned configuration selecting one exact :class:`NodeRef`.

    Absence of a row means unenrolled. Provider discovery must never create,
    mutate or delete these rows. Phase 5a1J adds only the schema and lifecycle;
    5a1H owns the verified writer and activation transaction.
    """

    class Mode(models.TextChoices):
        MANAGED = "managed", "Managed"
        SAFETY_ONLY = "safety_only", "Safety only"

    cluster = models.ForeignKey(
        ProxmoxCluster,
        on_delete=models.CASCADE,
        related_name="node_enrollments",
        # Covered by the exact NodeRef uniqueness constraint below.
        db_index=False,
    )
    node_name = models.CharField(max_length=120)
    node_ref_snapshot = models.CharField(max_length=188, editable=False)
    mode = models.CharField(max_length=16, choices=Mode.choices)
    enrolled_at = models.DateTimeField()
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    mode_changed_at = models.DateTimeField(null=True, blank=True)
    mode_changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    mode_change_reason = models.CharField(max_length=1000, blank=True, default="")
    onboarded_via_endpoint = models.ForeignKey(
        ProxmoxEndpoint,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cluster", "node_name"],
                name="core_cluster_node_enrollment_uniq",
            ),
            models.CheckConstraint(
                condition=~models.Q(node_name="") & ~models.Q(node_name__contains=":"),
                name="core_cluster_node_enrollment_valid_ref",
            ),
            models.CheckConstraint(
                condition=models.Q(mode__in=("managed", "safety_only")),
                name="core_cluster_node_enrollment_valid_mode",
            ),
            models.CheckConstraint(
                condition=~models.Q(node_ref_snapshot=""),
                name="core_cluster_node_enrollment_snapshot_nonempty",
            ),
        ]
        verbose_name = "cluster node enrollment"
        verbose_name_plural = "cluster node enrollments"

    def save(self, *args, **kwargs):
        """Create the serialized identity once and refuse later identity drift."""

        cluster_key = self.cluster.key
        expected_snapshot = NodeRef(cluster_key=cluster_key, node=self.node_name).serialize()
        if self._state.adding:
            if self.node_ref_snapshot and self.node_ref_snapshot != expected_snapshot:
                raise ValidationError({"node_ref_snapshot": "Node reference snapshot does not match the enrollment."})
            self.node_ref_snapshot = expected_snapshot
        else:
            original = type(self)._base_manager.only("cluster_id", "node_name", "node_ref_snapshot").get(pk=self.pk)
            if (
                self.cluster_id != original.cluster_id
                or self.node_name != original.node_name
                or self.node_ref_snapshot != original.node_ref_snapshot
            ):
                raise ValidationError("An enrollment's node identity and snapshot are immutable.")
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.node_ref_snapshot} ({self.mode})"
