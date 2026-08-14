"""Mutants for the Networks tab layering pass (5a4B-iii).

Run with `./scripts/mutation_pass.py docs/mutants/5a4b_iii.py`. One entry per
decision branch this pass added: how a row is bucketed, what order the buckets
render in, and when a bucket is allowed to collapse to a name strip.
"""

from __future__ import annotations

#: The panel composition and the two rendered scopes.
FAST = ["core.tests_workspace_networks"]

MUTANTS = [
    {
        "name": "sections: an unknown type is dropped instead of bucketed as other",
        "path": "core/services/workspace_networks.py",
        "old": '            key = _SECTION_BY_TYPE.get(row.interface_type.lower(), "other")',
        "new": '            key = _SECTION_BY_TYPE.get(row.interface_type.lower(), "vnet")',
        "modules": FAST,
    },
    {
        "name": "sections: type matching becomes case-sensitive, losing OVS*",
        "path": "core/services/workspace_networks.py",
        "old": "_SECTION_BY_TYPE.get(row.interface_type.lower(),",
        "new": "_SECTION_BY_TYPE.get(row.interface_type,",
        "modules": FAST,
    },
    {
        "name": "sections: stack order becomes the alphabet again",
        "path": "core/services/workspace_networks.py",
        "old": "            for key, title in _SECTION_TITLES",
        "new": "            for key, title in sorted(_SECTION_TITLES)",
        "modules": FAST,
    },
    {
        "name": "sections: empty layers render as empty headings",
        "path": "core/services/workspace_networks.py",
        "old": "            if key in buckets\n",
        "new": "            if buckets.setdefault(key, []) is not None\n",
        "modules": FAST,
    },
    {
        "name": "strip: any layer may collapse, not only physical ports",
        "path": "core/services/workspace_networks.py",
        "old": 'return self.key == "port" and all(_unremarkable(row) for row in self.interfaces)',
        "new": "return all(_unremarkable(row) for row in self.interfaces)",
        "modules": FAST,
    },
    {
        "name": "strip: one unremarkable row is enough to collapse the layer",
        "path": "core/services/workspace_networks.py",
        "old": 'return self.key == "port" and all(_unremarkable(row) for row in self.interfaces)',
        "new": 'return self.key == "port" and any(_unremarkable(row) for row in self.interfaces)',
        "modules": FAST,
    },
    {
        "name": "strip: a down port collapses like a live one",
        "path": "core/services/workspace_networks.py",
        "old": "        row.active\n        and row.present",
        "new": "        row.present",
        "modules": FAST,
    },
    {
        "name": "strip: an absent (hence unproven) port collapses",
        "path": "core/services/workspace_networks.py",
        "old": "        and row.present\n",
        "new": "",
        "modules": FAST,
    },
    {
        "name": "strip: a stale port collapses",
        "path": "core/services/workspace_networks.py",
        "old": "        and row.current\n",
        "new": "",
        "modules": FAST,
    },
    {
        "name": "strip: an attach target hides inside the strip",
        "path": "core/services/workspace_networks.py",
        "old": "        and not row.attachable\n",
        "new": "",
        "modules": FAST,
    },
    {
        "name": "strip: an addressed port collapses",
        "path": "core/services/workspace_networks.py",
        "old": "        and not row.address\n        and not row.cidr\n",
        "new": "",
        "modules": FAST,
    },
    {
        "name": "strip: a commented port collapses",
        "path": "core/services/workspace_networks.py",
        "old": "        and not row.comments\n",
        "new": "",
        "modules": FAST,
    },
    {
        "name": "template: the vnet section grows back the VLAN column it cannot answer",
        "path": "templates/core/partials/network_panel.html",
        "old": '                {% if section.key == "vnet" %}\n                  <th scope="col">Attachable</th>',
        "new": '                {% if section.key == "vnet" %}\n                  <th scope="col">VLAN</th>\n                  <th scope="col">Attachable</th>',
        "modules": FAST,
    },
    {
        "name": "template: the strip renders for every port section",
        "path": "templates/core/partials/network_panel.html",
        "old": "      {% if section.collapsible %}",
        "new": '      {% if section.key == "port" %}',
        "modules": FAST,
    },
    {
        "name": "template: the node scope stops rendering sections",
        "path": "templates/core/node_networks.html",
        "old": '  {% include "core/partials/network_panel.html" %}',
        "new": "",
        "modules": FAST,
    },
]
