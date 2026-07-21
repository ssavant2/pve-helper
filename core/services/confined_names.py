"""What may be used as a single component in a descriptor-relative path.

**This is deliberately a separate module from ``confined_filesystem``, and
merging it back has a cost that is not visible from the code.**

The syscalls in ``confined_filesystem._reserve_then_rename`` pass a name and a
``dir_fd``. The descriptor is the whole protection — the kernel resolves the name
against a directory this process already walked component by component with
``O_NOFOLLOW`` — but ``py/path-injection`` models the first argument as a path
sink and cannot see the descriptor beside it. The exception is expressed as a
barrier in the CodeQL model pack, and a barrier there resolves an *API-graph*
node: it matches a call that reaches a helper through an import, and not a call
made inside the module that defines the helper. Validating in place therefore
silences nothing, however thorough the validation is.

So the rule lives here, one import away, where the model can name it. That is an
odd reason for a module boundary and it is the real one: without this file the
five syscalls come back as findings in every local scan and in every GitHub code
scanning run. ``ConfinedFilesystemNameInvariantTests`` fails if it moves back.

Nothing else belongs here, and it imports nothing on purpose — a module the whole
confined boundary depends on should not be able to fail for a reason of its own.
"""

from __future__ import annotations


class ConfinedNameError(ValueError):
    """A name is not usable as a single confined path component."""


def confined_path_component(name: str) -> str:
    """Return ``name`` if it is one ordinary path component, else raise.

    Callers derive these from ``normalized_relative_path(...).parts``, so this
    cannot fail in practice — which is why it is stated rather than assumed. The
    descriptor-relative guarantee rests on the final argument of each syscall
    being a name that cannot walk anywhere; if that ever stops being true it
    should stop here rather than at the filesystem.
    """
    if not name or "/" in name or "\\" in name or "\x00" in name or name in {".", ".."}:
        raise ConfinedNameError("Path component is not a single ordinary name.")
    return name
