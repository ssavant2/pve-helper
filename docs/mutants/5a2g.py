"""Mutants for the Summary resource meters (5a2G).

Run with `./scripts/mutation_pass.py docs/mutants/5a2g.py`. One entry per decision
branch this phase added. Two of them carry the phase's whole point: a meter that
answers `percent` when it should not, and a cluster row that draws bars for a node
the totals beside it exclude.
"""

from __future__ import annotations

#: The Summary composition and both rendered scopes.
FAST = ["core.tests_workspace_shell"]

MUTANTS = [
    {
        "name": "meter: a value nobody reported counts as known",
        "path": "core/services/workspace_summary.py",
        "old": "        return self.used is not None and self.total is not None and self.total > 0",
        "new": "        return self.total is not None and self.total > 0",
        "modules": FAST,
    },
    {
        "name": "meter: a zero total is metered instead of refused",
        "path": "core/services/workspace_summary.py",
        "old": "and self.total is not None and self.total > 0",
        "new": "and self.total is not None",
        "modules": FAST,
    },
    {
        "name": "meter: unknown answers zero percent instead of nothing",
        "path": "core/services/workspace_summary.py",
        "old": "        if not self.known:\n            return None",
        "new": "        if not self.known:\n            return 0.0",
        "modules": FAST,
    },
    {
        "name": "meter: percent is the raw ratio, not a percentage",
        "path": "core/services/workspace_summary.py",
        "old": "        return 100.0 * self.used / self.total",
        "new": "        return self.used / self.total",
        "modules": FAST,
    },
    {
        "name": "cpu: usage without a core count is metered against nothing",
        "path": "core/services/workspace_summary.py",
        "old": "cpu=Meter(used=usage * cores if usage is not None and cores else None, total=cores),",
        "new": "cpu=Meter(used=usage, total=cores),",
        "modules": FAST,
    },
    {
        "name": "cpu: the cluster averages the fractions instead of weighting by cores",
        "path": "core/services/workspace_summary.py",
        "old": "            node.runtime.cpu_usage * node.runtime.cpu_cores\n",
        "new": "            node.runtime.cpu_usage\n",
        "modules": FAST,
    },
    {
        "name": "cpu: a node with no reading contributes zero busy cores",
        "path": "core/services/workspace_summary.py",
        "old": "            if node.runtime.cpu_usage is not None and node.runtime.cpu_cores\n",
        "new": "            if True\n",
        "modules": FAST,
    },
    {
        "name": "row: a node excluded from the totals draws its stale bars anyway",
        "path": "core/services/workspace_summary.py",
        "old": "        return node_meters(self.node.runtime) if self.node.runtime_current else None",
        "new": "        return node_meters(self.node.runtime)",
        "modules": FAST,
    },
    {
        "name": "row: no node ever gets bars",
        "path": "core/services/workspace_summary.py",
        "old": "        return node_meters(self.node.runtime) if self.node.runtime_current else None",
        "new": "        return None",
        "modules": FAST,
    },
    {
        "name": "node page: withholds its bars the way the cluster row does",
        "path": "core/services/workspace_summary.py",
        "old": "    def meters(self) -> NodeMeters:\n",
        "new": "    def meters(self) -> NodeMeters | None:\n        if not self.node.runtime_current:\n            return None\n",
        "modules": FAST,
    },
    {
        "name": "template: an unknown meter draws an empty track",
        "path": "templates/core/partials/meter.html",
        "old": '  {% if meter.known %}\n    <div class="meter-track"',
        "new": '  {% if True %}\n    <div class="meter-track"',
        "modules": FAST,
    },
    {
        "name": "template: an unknown meter says nothing rather than saying so",
        "path": "templates/core/partials/meter.html",
        "old": '      <span class="meter-value muted">not reported</span>',
        "new": '      <span class="meter-value muted">-</span>',
        "modules": FAST,
    },
    {
        "name": "template: the cluster row stops explaining the missing cells",
        "path": "templates/core/cluster_summary.html",
        "old": '                    <td colspan="3" class="muted">Not counted while runtime is {{ row.node.runtime_status }}.</td>',
        "new": '                    <td colspan="3" class="muted">-</td>',
        "modules": FAST,
    },
    {
        "name": "template: Cluster Summary loses its memory bar",
        "path": "templates/core/cluster_summary.html",
        "old": '          {% include "core/partials/meter.html" with meter=summary.capacity.memory label="Memory" used_text=summary.capacity.memory_used_bytes|filesizeformat total_text=summary.capacity.memory_total_bytes|filesizeformat %}\n',
        "new": "",
        "modules": FAST,
    },
    {
        "name": "template: Node Summary loses its memory meter",
        "path": "templates/core/node_summary.html",
        "old": '        {% include "core/partials/meter.html" with meter=summary.meters.memory label="Memory" used_text=node.runtime.memory_used_bytes|filesizeformat total_text=node.runtime.memory_total_bytes|filesizeformat %}\n',
        "new": "",
        "modules": FAST,
    },
]
