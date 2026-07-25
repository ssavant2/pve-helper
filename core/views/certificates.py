"""Settings → Certificates: the HTTPS certificate, trusted authorities, expiry policy.

Every mutation here republishes the shared volume before returning. Doing it at the
end of each action rather than on a timer means the page the operator sees after a
redirect already reflects what nginx is about to load, and the reload watcher has
something to notice.
"""

from __future__ import annotations

import logging

from django.conf import settings as django_settings
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.models import CertificateSettings, ManagedCertificate
from core.services.audit_events import record_audit_event
from core.services.certificate_store import publish
from core.services.certificates import (
    MAX_EXPIRY_WARNING_DAYS,
    MAX_UPLOAD_BYTES,
    MIN_EXPIRY_WARNING_DAYS,
    CertificateError,
    certificate_in_use,
    days_until_expiry,
    delete_certificate,
    import_certificate,
    select_https_certificate,
    settings_record,
    update_expiry_policy,
)
from core.services.public_errors import PublicMessageError

from .common import app_login_required, navigation_context

logger = logging.getLogger(__name__)

CERTIFICATE_FIELDS = ("certificate", "chain", "private_key")


@app_login_required
def certificate_settings(request):
    errors: list[str] = []
    if request.method == "POST":
        try:
            _handle(request)
        except PublicMessageError as exc:
            errors.append(exc.public_message)
        else:
            return redirect("core:settings_certificates")

    config = settings_record()
    return render(
        request,
        "core/settings_certificates.html",
        {
            **navigation_context("pve_settings", page_title=("Certificates", "Settings")),
            "active_settings_tab": "certificates",
            "errors": errors,
            "config": config,
            "server_certificates": _rows(ManagedCertificate.Usage.SERVER, config),
            "authorities": _rows(ManagedCertificate.Usage.AUTHORITY, config),
            "force_http": _force_http(),
            "min_warning_days": MIN_EXPIRY_WARNING_DAYS,
            "max_warning_days": MAX_EXPIRY_WARNING_DAYS,
            "max_upload_kib": MAX_UPLOAD_BYTES // 1024,
        },
    )


def _force_http() -> bool:
    return bool(getattr(django_settings, "APP_FORCE_HTTP", False))


def _handle(request) -> None:
    action = str(request.POST.get("action") or "")
    if action == "upload":
        _upload(request)
    elif action == "select_https":
        _select_https(request)
    elif action == "delete":
        _delete(request)
    elif action == "expiry_policy":
        _expiry_policy(request)
    else:
        raise CertificateError("Unknown action.")


def _uploaded_blobs(request) -> tuple[list[bytes], str]:
    blobs: list[bytes] = []
    names: list[str] = []
    for field in CERTIFICATE_FIELDS:
        uploaded = request.FILES.get(field)
        if uploaded is None:
            continue
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise CertificateError(f"{uploaded.name} is larger than {MAX_UPLOAD_BYTES // 1024} KiB.")
        blobs.append(uploaded.read())
        names.append(uploaded.name)
    if not blobs:
        raise CertificateError("Choose a certificate file to upload.")
    return blobs, ", ".join(names)[:255]


def _upload(request) -> None:
    # Read once: an UploadedFile is a stream, and reading it a second time yields
    # nothing rather than the same bytes.
    blobs, filenames = _uploaded_blobs(request)
    record = import_certificate(
        usage=str(request.POST.get("usage") or ""),
        label=str(request.POST.get("label") or ""),
        blobs=blobs,
        password=str(request.POST.get("password") or ""),
        source_filename=filenames,
        uploaded_by=request.user.get_username() if request.user.is_authenticated else "",
    )
    publish()
    record_audit_event(
        request=request,
        action="certificate.imported",
        object_type="certificate",
        object_id=str(record.pk),
        details={
            "usage": record.usage,
            "label": record.label,
            "subject": record.subject,
            "issuer": record.issuer,
            "fingerprint": record.sha256_fingerprint,
            "not_after": record.not_after.isoformat() if record.not_after else "",
        },
    )


def _select_https(request) -> None:
    enabled = request.POST.get("https_enabled") == "on"
    raw = str(request.POST.get("certificate_id") or "").strip()
    record = None
    if raw:
        record = get_object_or_404(ManagedCertificate, pk=raw, usage=ManagedCertificate.Usage.SERVER)
    select_https_certificate(record, enabled=enabled)
    publish()
    record_audit_event(
        request=request,
        action="certificate.https.updated",
        object_type="certificate",
        object_id=str(record.pk) if record else "",
        details={
            "https_enabled": enabled,
            "label": record.label if record else "",
            "fingerprint": record.sha256_fingerprint if record else "",
        },
    )


def _delete(request) -> None:
    record = get_object_or_404(ManagedCertificate, pk=str(request.POST.get("certificate_id") or "").strip())
    label, usage, fingerprint = record.label, record.usage, record.sha256_fingerprint
    try:
        delete_certificate(record)
    except ProtectedError as exc:
        # PROTECT is the database's own copy of the same rule the service enforces.
        # Reaching it means the two disagreed, which is worth a log line.
        logger.warning("Refused to delete certificate %s still referenced by settings", record.pk)
        raise CertificateError("That certificate is still in use and was not removed.") from exc
    publish()
    record_audit_event(
        request=request,
        action="certificate.deleted",
        object_type="certificate",
        object_id="",
        details={"usage": usage, "label": label, "fingerprint": fingerprint},
    )


def _expiry_policy(request) -> None:
    try:
        days = int(str(request.POST.get("expiry_warning_days") or ""))
    except ValueError as exc:
        raise CertificateError("The warning window must be a whole number of days.") from exc
    config = update_expiry_policy(enabled=request.POST.get("expiry_warning_enabled") == "on", days=days)
    record_audit_event(
        request=request,
        action="certificate.expiry_policy.updated",
        object_type="certificate",
        object_id="",
        details={"enabled": config.expiry_warning_enabled, "days": config.expiry_warning_days},
    )


def _rows(usage: str, config: CertificateSettings) -> list[dict[str, object]]:
    now = timezone.now()
    threshold = int(config.expiry_warning_days)
    rows: list[dict[str, object]] = []
    for record in ManagedCertificate.objects.filter(usage=usage):
        remaining = days_until_expiry(record, now=now)
        expired = remaining is not None and remaining < 0
        rows.append(
            {
                "record": record,
                "in_use": certificate_in_use(record),
                "days_remaining": remaining,
                "expired": expired,
                "expiring": bool(
                    config.expiry_warning_enabled and not expired and remaining is not None and remaining <= threshold
                ),
                "not_after": timezone.localtime(record.not_after).strftime("%Y-%m-%d %H:%M")
                if record.not_after
                else "",
                "names": ", ".join(record.subject_alt_names) if record.subject_alt_names else "",
                "fingerprint": record.sha256_fingerprint,
            }
        )
    return rows
