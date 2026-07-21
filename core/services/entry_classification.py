"""The single place where a scanned storage entry becomes a classification.

There are two scan paths — the full scan in `core.tasks` and the partial
directory refresh in `core.services.partial_scan` — and they must reach the same
verdict about the same file. They did not. The full scan overrode the legacy
`classify_entry` result with the API storage catalog for disk images; the partial
refresh only ever called `classify_entry`. So a disk the catalog knew is
referenced came back `classification-blocked` after any rename, move, trash or
restore, until the next full scan happened to undo it.

That is a drift defect, not a missing call, so the fix is to leave exactly one
implementation to drift from. `ScanEntryClassifierInvariantTests` fails if either
scan path classifies on its own again.
"""

from __future__ import annotations

import logging

from core.models import StorageMount

from .classification import ClassificationResult, classify_entry
from .storage import StorageEntry
from .storage_catalog import MountedVolumeClassifier

logger = logging.getLogger(__name__)

# Categories where the API storage catalog knows more than a volid match against
# one scan's inventory rows, and is therefore allowed to overrule it.
CATALOG_AUTHORITATIVE_CATEGORIES = frozenset({"vm_disk", "base_image"})


class ScanEntryClassifier:
    """Classifies every entry of one storage mount within one scan.

    Everything except the entry itself is fixed for the whole mount: the scan's
    guest references, the storage gate verdict, and the catalog's bindings and
    volume observations. It is resolved once here, when the classifier is built —
    resolving it per file made a scan quadratic in datastore size.
    """

    def __init__(
        self,
        *,
        storage: StorageMount,
        referenced_volids: set[str],
        template_vmids: set[int],
        gate_ok: bool,
        missing_consumers: list[str],
    ) -> None:
        self._storage = storage
        self._referenced_volids = referenced_volids
        self._template_vmids = template_vmids
        self._gate_ok = gate_ok
        self._missing_consumers = missing_consumers
        self._volume_classifier = MountedVolumeClassifier(storage)

    def classify(self, entry: StorageEntry) -> ClassificationResult:
        legacy = classify_entry(
            relative_path=entry.relative_path,
            entry_type=entry.entry_type,
            content_category=entry.content_category,
            derived_volid=entry.derived_volid,
            referenced_volids=self._referenced_volids,
            template_vmids=self._template_vmids,
            gate_ok=self._gate_ok,
            missing_consumers=self._missing_consumers,
        )
        if entry.content_category not in CATALOG_AUTHORITATIVE_CATEGORIES:
            return legacy

        catalog = self._volume_classifier.classify(entry.relative_path)
        if catalog is None:
            return legacy

        catalog.evidence["comparison"] = {
            "legacy": legacy.classification,
            "catalog": catalog.classification,
            "matched": legacy.classification == catalog.classification,
        }
        if legacy.classification != catalog.classification:
            logger.info(
                "Storage classification comparison differs: mount=%s path=%s legacy=%s catalog=%s",
                self._storage.mount_ref,
                entry.relative_path,
                legacy.classification,
                catalog.classification,
            )
        return catalog
