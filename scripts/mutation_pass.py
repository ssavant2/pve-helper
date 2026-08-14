#!/usr/bin/env python3
"""Run a phase's mutation pass: delete each decision branch, prove a test fails.

`AGENTS.md` makes mutation the exit criterion for a Module 5 phase — "every new
decision branch has a test that fails when the branch is deleted" — and a green
suite under a mutant means the test is missing, not that the code is safe. This
runs that measurement.

    ./scripts/mutation_pass.py docs/mutants/5a4b_ii.py        # every mutant
    ./scripts/mutation_pass.py docs/mutants/5a4b_ii.py 3 7    # only those two

The spec is a Python file defining `MUTANTS`, a list of dicts:

    MUTANTS = [
        {
            "name": "bridges: tombstone offered",
            "path": "core/services/cluster_projection_read.py",
            "old": "if row.attachable and row.present",   # must occur exactly once
            "new": "if row.attachable",
            "modules": ["core.tests_workspace_networks"],
        },
    ]

Three things this does that a hand-rolled loop gets wrong, each learned the hard
way on 5a4B-ii:

**It restores the tree on every exit path, signals included.** A run interrupted
between applying a mutant and restoring it leaves the mutation sitting in a source
file, where it will be committed or silently corrupt the next run's baseline. That
happened twice; hence the signal handlers and `atexit` below.

**It refuses to start unless every anchor is present exactly once.** A missing
anchor is either a stale spec or — worse — a mutant left behind by an earlier run.
Both mean the results would be fiction, so the pass does not begin.

**It picks test modules per mutant.** Running the whole heavy set for every mutant
was the difference between seven seconds and a minute per mutant. A mutant is
killed by a specific test; name the module that holds it.

`--no-deps` matters for the same reason: without it Compose recreates the `migrate`
service on every single run. The bind mounts are `core/` and `templates/` rather
than `/app`, so the image's collected staticfiles manifest stays visible and
template-rendering tests can run without a rebuild per mutant.
"""

from __future__ import annotations

import atexit
import importlib.util
import pathlib
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent

_pristine: dict[pathlib.Path, str] = {}


def _restore_all(*_args: object) -> None:
    for path, text in _pristine.items():
        if path.read_text() != text:
            path.write_text(text)
    _pristine.clear()


atexit.register(_restore_all)
for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, lambda *_a: sys.exit(130))


def load_mutants(spec_path: str) -> list[dict]:
    path = pathlib.Path(spec_path)
    spec = importlib.util.spec_from_file_location("mutation_spec", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Not an importable mutation spec: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return list(module.MUTANTS)


def preflight(mutants: list[dict]) -> bool:
    """Every anchor present exactly once, or the results would be fiction."""

    broken = [
        f"[{index}] {mutant['path']}: anchor found {(ROOT / mutant['path']).read_text().count(mutant['old'])}x"
        f" -- {mutant['name']}"
        for index, mutant in enumerate(mutants)
        if (ROOT / mutant["path"]).read_text().count(mutant["old"]) != 1
    ]
    if broken:
        print(
            "REFUSING TO RUN -- the tree is not pristine. Either the spec is stale, or an\n"
            "earlier interrupted run left a mutant in the file. Check both:\n" + "\n".join(broken)
        )
        return False
    return True


def _db_admin_credentials() -> dict[str, str]:
    """The DB admin role, read from Compose so nothing is echoed or hard-coded."""

    config = subprocess.run(
        ["docker", "compose", "config", "--environment"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    values = dict(
        line.split("=", 1) for line in config.splitlines() if line.startswith(("DB_ADMIN_USER=", "DB_ADMIN_PASSWORD="))
    )
    if len(values) != 2:
        raise SystemExit("DB_ADMIN_USER/DB_ADMIN_PASSWORD are missing from the Compose environment")
    return values


def run_tests(modules: list[str], credentials: dict[str, str]) -> set[str]:
    """The named modules against the working tree; the failing test names."""

    result = subprocess.run(
        [
            "docker", "compose", "run", "--rm", "--no-deps",
            "-v", f"{ROOT}/core:/app/core",
            "-v", f"{ROOT}/templates:/app/templates",
            "-e", f"DB_USER={credentials['DB_ADMIN_USER']}",
            "-e", f"DB_PASSWORD={credentials['DB_ADMIN_PASSWORD']}",
            "web", "python", "manage.py", "test", *modules,
            "--settings=pve_helper.test_settings", "--keepdb",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )  # fmt: skip
    output = result.stdout + result.stderr
    return {line for line in output.splitlines() if line.startswith(("FAIL: ", "ERROR: "))}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    mutants = load_mutants(argv[0])
    selected = {int(value) for value in argv[1:]}
    if not preflight(mutants):
        return 1

    credentials = _db_admin_credentials()
    baselines: dict[tuple[str, ...], set[str]] = {}
    killed: list[str] = []
    survived: list[str] = []

    for index, mutant in enumerate(mutants):
        if selected and index not in selected:
            continue
        modules = list(mutant["modules"])
        key = tuple(modules)
        if key not in baselines:
            baselines[key] = run_tests(modules, credentials)
            if baselines[key]:
                print(f"BASELINE NOT GREEN for {key}: {sorted(baselines[key])}")
                return 1
        path = ROOT / mutant["path"]
        _pristine[path] = path.read_text()
        path.write_text(_pristine[path].replace(mutant["old"], mutant["new"]))
        started = time.time()
        try:
            failures = run_tests(modules, credentials) - baselines[key]
        finally:
            _restore_all()
        if failures:
            killed.append(mutant["name"])
            witness = sorted(failures)[0].split("(")[0].strip()
            print(
                f"[{index}] KILLED ({time.time() - started:.0f}s)  {mutant['name']}\n          by {witness}", flush=True
            )
        else:
            survived.append(mutant["name"])
            print(f"[{index}] SURVIVED  {mutant['name']}  <-- the branch has no test", flush=True)

    print(f"\n{len(killed)} killed, {len(survived)} survived")
    return 0 if not survived else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
