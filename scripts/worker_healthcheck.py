"""Container health check for the Django-Q worker.

Django-Q stores its cluster heartbeat in the local-memory cache, which is
per-process and therefore not visible to a separate health-check process. So
instead of querying cluster stats, verify that a ``qcluster`` process is alive
inside the container's PID namespace (the worker container runs nothing else).

Exit 0 = healthy, 1 = no qcluster process found.
"""

import os
import sys


def qcluster_running() -> bool:
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                if b"qcluster" in handle.read():
                    return True
        except OSError:
            continue
    return False


sys.exit(0 if qcluster_running() else 1)
