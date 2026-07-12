"""Guest (VM/CT) views, split into a package. ``_core`` still holds the bulk;
seams are extracted into sibling modules over time. Everything is re-exported so
``views.<name>`` (URL routing) and ``core.views.guests.<name>`` keep resolving.

During the split this package is a transparent facade over its submodules: the
public API comes through ``import *``, and the private ``_helpers`` that tests and
``template_clone_views`` import by name are surfaced explicitly below. Note that
patch targets must point at the module that *defines* a name (e.g.
``core.views.guests._core._require_guest``), not at this facade, or the patch
won't intercept intra-module calls.
"""

from . import _core, firewall, console, tabs, replication, panels, hardware, read_models, dialogs, mutations
from ._core import *  # noqa: F401,F403
from .firewall import *  # noqa: F401,F403
from .console import *  # noqa: F401,F403
from .tabs import *  # noqa: F401,F403
from .replication import *  # noqa: F401,F403
from .panels import *  # noqa: F401,F403
from .hardware import *  # noqa: F401,F403
from .read_models import *  # noqa: F401,F403
from .dialogs import *  # noqa: F401,F403
from .mutations import *  # noqa: F401,F403


def _surface_private(module):
    """Re-export a submodule's single-underscore helpers onto the package so
    ``from core.views.guests import _helper`` keeps working during the split."""
    globals().update(
        {name: value for name, value in vars(module).items() if name.startswith("_") and not name.startswith("__")}
    )


_surface_private(_core)
_surface_private(firewall)
_surface_private(console)
_surface_private(tabs)
_surface_private(replication)
_surface_private(panels)
_surface_private(hardware)
_surface_private(read_models)
_surface_private(dialogs)
_surface_private(mutations)
