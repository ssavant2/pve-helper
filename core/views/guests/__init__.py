"""Public VM/CT view facade used by URL routing.

Private helpers are not discovered dynamically. The small explicit compatibility
surface below exists only for older direct imports; new code imports the module
that owns the helper.
"""

from . import _core, firewall, console, tabs, replication, panels, hardware, read_models, dialogs, mutations, actions, create, operation_lifecycle, presenters, read_model_support
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
from .actions import *  # noqa: F401,F403
from .create import *  # noqa: F401,F403
from .operation_lifecycle import *  # noqa: F401,F403
from .presenters import *  # noqa: F401,F403
from .read_model_support import *  # noqa: F401,F403

# Explicit legacy imports. Tests and production helpers should migrate away
# from this list instead of extending it with another dynamic private export.
from ._core import _guest_cpu_model, _guest_movable_disks, _guest_nic_bridges  # noqa: F401,E402
from .actions import _migrate_guest_from_bulk_request  # noqa: F401,E402
from .read_model_support import (  # noqa: F401,E402
    _apply_workspace_lineage,
    _display_lock,
    _guest_health,
    _linked_clone_disk_edit_block,
    _mark_linked_clones,
)
