"""Mutants for phase 5a4B-ii — Networks tabs and consumer migration.

Run with `./scripts/mutation_pass.py docs/mutants/5a4b_ii.py`. Kept as the worked
example the next phase copies: one entry per decision branch the phase added,
each naming the smallest test module that can kill it.

Result on 2026-08-14: 27 killed, 0 survived.
"""

from __future__ import annotations

#: The read/seam/panel tests. Seven seconds a run.
FAST = ["core.tests_node_networks", "core.tests_workspace_networks"]
#: Adds the rendered bridge pickers. Thirty seconds a run, so only where needed.
FORMS = [*FAST, "core.tests.ViewSmokeTests"]
#: The source-invariant ratchets.
RATCHET = ["core.tests_source_invariants"]

MUTANTS = [
    {
        "name": "boundary: publication check removed",
        "path": "core/services/cluster_projection_read.py",
        "old": "    if not published:\n",
        "new": "    if False:\n",
        "modules": FAST,
    },
    {
        "name": "status: missing coverage treated as current",
        "path": "core/services/cluster_projection_read.py",
        "old": "    if coverage is None:\n        status = NodeNetworkReadStatus.MISSING",
        "new": "    if False:\n        status = NodeNetworkReadStatus.MISSING",
        "modules": FAST,
    },
    {
        "name": "status: incomplete coverage treated as current",
        "path": "core/services/cluster_projection_read.py",
        "old": "    elif not coverage.complete:\n        status = NodeNetworkReadStatus.FAILED",
        "new": "    elif False:\n        status = NodeNetworkReadStatus.FAILED",
        "modules": FAST,
    },
    {
        "name": "row currency: generation equality dropped",
        "path": "core/services/cluster_projection_read.py",
        "old": "            current=bool(generation and row.observed_generation == generation),",
        "new": "            current=True,",
        "modules": FAST,
    },
    {
        "name": "bridges: unknown no longer withholds the list",
        "path": "core/services/cluster_projection_read.py",
        "old": "        if not self.known:\n            return ()",
        "new": "        if False:\n            return ()",
        "modules": FAST,
    },
    {
        "name": "bridges: attachable flag ignored",
        "path": "core/services/cluster_projection_read.py",
        "old": "                if row.attachable and row.present and not row.unreachable and row.current",
        "new": "                if row.present and not row.unreachable and row.current",
        "modules": FAST,
    },
    {
        "name": "bridges: tombstone offered",
        "path": "core/services/cluster_projection_read.py",
        "old": "                if row.attachable and row.present and not row.unreachable and row.current",
        "new": "                if row.attachable and not row.unreachable and row.current",
        "modules": FAST,
    },
    {
        "name": "bridges: unreachable row offered",
        "path": "core/services/cluster_projection_read.py",
        "old": "                if row.attachable and row.present and not row.unreachable and row.current",
        "new": "                if row.attachable and row.present and row.current",
        "modules": FAST,
    },
    {
        "name": "bridges: stale-generation row offered",
        "path": "core/services/cluster_projection_read.py",
        "old": "                if row.attachable and row.present and not row.unreachable and row.current",
        "new": "                if row.attachable and row.present and not row.unreachable",
        "modules": FAST,
    },
    {
        "name": "reason: never-swept collapses into the generic fallback",
        "path": "core/services/node_networks.py",
        "old": "    if read.status is NodeNetworkReadStatus.MISSING:\n        return _MISSING_REASON",
        "new": "    if False:\n        return _MISSING_REASON",
        "modules": FAST,
    },
    {
        "name": "reason: hidden node loses its own sentence",
        "path": "core/services/node_networks.py",
        "old": '    if read.status is NodeNetworkReadStatus.NOT_PUBLISHED:\n        return _REFUSAL_REASONS["node_not_published"]',
        "new": '    if False:\n        return _REFUSAL_REASONS["node_not_published"]',
        "modules": FAST,
    },
    {
        "name": "reason: an unmapped code says nothing",
        "path": "core/services/node_networks.py",
        "old": "    return _REFUSAL_REASONS.get(code, _UNMAPPED_REASON)",
        "new": '    return _REFUSAL_REASONS.get(code, "")',
        "modules": FAST,
    },
    {
        "name": "seam: a retired cluster raises instead of refusing",
        "path": "core/services/node_networks.py",
        "old": "    except ClusterProjectionNotFound:",
        "new": "    except ZeroDivisionError:",
        "modules": FAST,
    },
    {
        "name": "seam: bulk read drops no requested node",
        "path": "core/services/node_networks.py",
        "old": "    return {read.node_name: _bridges(read) for read in reads}",
        "new": "    return {read.node_name: _bridges(read) for read in reads if read.known}",
        "modules": FAST,
    },
    {
        "name": "create form: bridges_known hardcoded true",
        "path": "core/services/guest_create.py",
        "old": '        "bridges_known": bridges.known,',
        "new": '        "bridges_known": True,',
        "modules": FORMS,
    },
    {
        "name": "panel: hidden nodes rendered as groups",
        "path": "core/services/workspace_networks.py",
        "old": "    wanted = [node] if node else [name for name in members if scope.publishes(name)]",
        "new": "    wanted = [node] if node else list(members)",
        "modules": FAST,
    },
    {
        "name": "panel: unread members no longer footnoted",
        "path": "core/services/workspace_networks.py",
        "old": "        unread_nodes=tuple(sorted(name for name in members if not scope.observes(name))),",
        "new": "        unread_nodes=(),",
        "modules": FAST,
    },
    {
        "name": "panel: an unreachable row counted as gone",
        "path": "core/services/workspace_networks.py",
        "old": "        return tuple(row for row in self.interfaces if not row.present and not row.unreachable)",
        "new": "        return tuple(row for row in self.interfaces if not row.present)",
        "modules": FAST,
    },
    {
        "name": "panel: attachable ignores the tombstone",
        "path": "core/services/workspace_networks.py",
        "old": "        return tuple(row for row in self.interfaces if row.attachable and row.present)",
        "new": "        return tuple(row for row in self.interfaces if row.attachable)",
        "modules": FAST,
    },
    {
        "name": "dialog: an unknown node stays a selectable target",
        "path": "core/views/guests/dialogs.py",
        "old": "            if answer is not None and answer.known:\n                continue",
        "new": "            if True:\n                continue",
        "modules": FAST,
    },
    {
        "name": "dialog: an unknown node overwrites an existing block reason",
        "path": "core/views/guests/dialogs.py",
        "old": '            if entry["allowed"]:\n                entry["allowed"] = False',
        "new": '            if True:\n                entry["allowed"] = False',
        "modules": FAST,
    },
    {
        "name": "view: a cluster retired mid-request 500s instead of 404ing",
        "path": "core/views/clusters/workspace.py",
        "old": '    try:\n        return network_panel(cluster, **kwargs)\n    except ClusterProjectionNotFound as exc:\n        raise Http404("Proxmox cluster not found") from exc',
        "new": "    return network_panel(cluster, **kwargs)",
        "modules": FAST,
    },
    {
        "name": "template: the unknown-network notice is not rendered",
        "path": "templates/core/partials/bridge_picker_notice.html",
        "old": "{% if options.available and not options.bridges_known %}",
        "new": "{% if False %}",
        "modules": FORMS,
    },
    {
        "name": "template: the hardware form drops a bridge it cannot confirm",
        "path": "templates/core/guest_hardware_edit.html",
        "old": "{% if nic.bridge and nic.bridge not in options.bridges %}",
        "new": "{% if False %}",
        "modules": FORMS,
    },
    {
        "name": "template: the hardware form stops including the notice",
        "path": "templates/core/guest_hardware_edit.html",
        "old": '                {% include "core/partials/bridge_picker_notice.html" %}\n',
        "new": "",
        "modules": FORMS,
    },
    {
        "name": "template: the Networks tab renders an unknown node as an empty table",
        "path": "templates/core/partials/network_panel.html",
        "old": "    {% if not group.known %}",
        "new": "    {% if False %}",
        "modules": FAST,
    },
    {
        "name": "ratchet: the node-network provider domain is no longer detected",
        "path": "core/tests_source_invariants.py",
        "old": '                if re.search(r"(?:^|/)nodes/(?:\\{\\}|[^/]+)/network(?:$|\\?|/)", shape):\n                    domains.add("node_network")',
        "new": '                if False:\n                    domains.add("node_network")',
        "modules": RATCHET,
    },
]
