"""Mutants for the 24-hour clock and the observation-age lines.

Run with `./scripts/mutation_pass.py docs/mutants/clock_age.py`.

The harness bind-mounts `core/` and `templates/` only, so `FORMAT_MODULE_PATH`
and `pve_helper/formats/` cannot be mutated here — a change on disk would not
reach the container and would read as a survivor. That branch is covered instead
by `test_the_default_datetime_format_is_the_one_the_app_writes_by_hand`, which
asserts both the app's format and the `en` locale default it replaces.
"""

from __future__ import annotations

#: The age helper and both rendered scopes.
FAST = ["core.tests_workspace_shell"]

MUTANTS = [
    {
        "name": "age: under a minute is reported as zero minutes",
        "path": "core/services/durations.py",
        "old": '    if seconds < SECONDS_PER_MINUTE:\n        return "just now"',
        "new": '    if seconds < 0:\n        return "just now"',
        "modules": FAST,
    },
    {
        "name": "age: a future timestamp becomes a negative age",
        "path": "core/services/durations.py",
        "old": "    if seconds < SECONDS_PER_MINUTE:",
        "new": "    if 0 <= seconds < SECONDS_PER_MINUTE:",
        "modules": FAST,
    },
    {
        "name": "age: hours are reported in minutes",
        "path": "core/services/durations.py",
        "old": '    if seconds < SECONDS_PER_DAY:\n        return _plural(seconds // SECONDS_PER_HOUR, "hour")',
        "new": '    if seconds < SECONDS_PER_DAY:\n        return _plural(seconds // SECONDS_PER_MINUTE, "minute")',
        "modules": FAST,
    },
    {
        "name": "age: a single unit is still pluralised",
        "path": "core/services/durations.py",
        "old": "    return f\"{count} {unit}{'' if count == 1 else 's'} ago\"",
        "new": '    return f"{count} {unit}s ago"',
        "modules": FAST,
    },
    {
        "name": "age: an absent timestamp is rendered as an age anyway",
        "path": "core/services/durations.py",
        "old": '    if when is None:\n        return ""',
        "new": '    if when is None:\n        return "just now"',
        "modules": FAST,
    },
    {
        "name": "filter: the age filter stops reaching the helper",
        "path": "core/templatetags/time_labels.py",
        "old": "    return format_age(when)",
        "new": '    return str(when or "")',
        "modules": FAST,
    },
    {
        "name": "node page: a failed-only read is drawn as an observation",
        "path": "templates/core/node_summary.html",
        "old": "      {% if node.runtime_coverage.observed_at %}",
        "new": "      {% if node.runtime_coverage %}",
        "modules": FAST,
    },
    {
        "name": "node page: never-published and never-observed collapse into one",
        "path": "templates/core/node_summary.html",
        "old": "This node's runtime has been attempted but never observed.",
        "new": "This node's runtime has never been published.",
        "modules": FAST,
    },
    {
        "name": "node page: the absolute timestamp goes away",
        "path": "templates/core/node_summary.html",
        "old": '          <span class="muted">({{ node.runtime_coverage.observed_at }})</span>\n',
        "new": "",
        "modules": FAST,
    },
    {
        "name": "cluster page: the membership age goes away",
        "path": "templates/core/cluster_summary.html",
        "old": "              Observed {{ projection.membership_coverage.observed_at|age }}",
        "new": "              Observed",
        "modules": FAST,
    },
    {
        "name": "cluster page: the reading node stops being named",
        "path": "templates/core/cluster_summary.html",
        "old": "{% if projection.observed_from %} from {{ projection.observed_from }}{% endif %}",
        "new": "",
        "modules": FAST,
    },
    {
        "name": "cluster page: the absolute timestamp goes away",
        "path": "templates/core/cluster_summary.html",
        "old": '              <span class="muted">({{ projection.membership_coverage.observed_at }})</span>\n',
        "new": "",
        "modules": FAST,
    },
]
