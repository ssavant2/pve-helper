"""What a guest NIC may attach to on a given node.

Two surfaces ask this question — the migrate dialog's per-target check and the
create form's bridge list — and they used to answer it independently. Both were
wrong against live data, in opposite directions, which is the drift this module
exists to prevent: one reader, one verdict.

``GET nodes/<node>/network?type=any_bridge`` is the provider's own answer. It
returns Linux/OVS bridges together with the SDN vnets actually realized on that
node, and it honours a zone's node restriction — a vnet in a zone scoped to
``pve1,pve2`` is simply absent from pve3's answer.

The obvious-looking alternative, filtering the plain interface listing by type and
merging in ``cluster/sdn/vnets``, does not reproduce it and cannot be repaired:

* a realized vnet with no address is **absent** from the plain listing entirely,
  so the merge under-reports;
* a realized vnet that does have an address comes back ``type=unknown`` there, so
  type alone cannot classify it;
* ``cluster/sdn/vnets`` is cluster-scoped and carries no node opinion, so merging
  it in over-reports onto nodes whose zone excludes the vnet.

Once 5a4B-i publishes the node-network projection this becomes a database read.
The seam is here so that migration changes one function, not two call sites.
"""

from __future__ import annotations

from urllib.parse import quote

from core.services.proxmox import ProxmoxAPIError


def node_attachable_bridges(client, node: str) -> list[str]:
    """Sorted interface names a NIC can attach to on ``node``.

    Returns an empty list when the node cannot answer. That is *unknown*, not
    "no bridges"; a caller that renders it must not present it as proven absence.
    """
    if not node:
        return []
    try:
        raw = client.get(f"nodes/{quote(node, safe='')}/network?type=any_bridge")
    except ProxmoxAPIError:
        return []
    if not isinstance(raw, list):
        return []
    return sorted({str(iface["iface"]) for iface in raw if isinstance(iface, dict) and iface.get("iface")})
