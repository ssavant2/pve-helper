import { selectedVmOverviewRows, visibleVmOverviewRows, vmOverviewRows } from "./scheduling.js";
import { applyIpVersionStyle, createIcons, ipVersion, registerPageCleanup, renderIpCell } from "./shell.js";

const syncVmOverviewSelection = (overview) => {
  const rows = vmOverviewRows(overview);
  rows.forEach((row) => {
    row.classList.toggle("selected", Boolean(row.querySelector("[data-vm-select]")?.checked));
  });

  const selectedRows = selectedVmOverviewRows(overview);
  const visibleRows = visibleVmOverviewRows(overview);
  const selectedVisibleCount = visibleRows.filter((row) => row.querySelector("[data-vm-select]")?.checked).length;
  const selectAll = overview.querySelector("[data-vm-select-all]");
  if (selectAll) {
    selectAll.checked = visibleRows.length > 0 && selectedVisibleCount === visibleRows.length;
    selectAll.indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < visibleRows.length;
  }

  const status = overview.querySelector("[data-vm-selection-status]");
  if (status) {
    status.textContent = `${selectedRows.length} selected`;
  }
};

const applyStoredSortForOverview = (overview) => {
  const table = overview?.querySelector("[data-sortable-table]");
  if (table && typeof table.pveHelperApplyStoredSort === "function") {
    table.pveHelperApplyStoredSort();
  }
};

const initVmOverviewSelection = (root = document) => {
  root.querySelectorAll("[data-vm-overview]").forEach((overview) => {
    if (overview.dataset.selectionInitialized === "true") {
      syncVmOverviewSelection(overview);
      return;
    }
    overview.dataset.selectionInitialized = "true";

    overview.addEventListener("change", (event) => {
      const selectAll = event.target.closest("[data-vm-select-all]");
      if (selectAll && overview.contains(selectAll)) {
        visibleVmOverviewRows(overview).forEach((row) => {
          const checkbox = row.querySelector("[data-vm-select]");
          if (checkbox) {
            checkbox.checked = selectAll.checked;
          }
        });
        syncVmOverviewSelection(overview);
        return;
      }

      const checkbox = event.target.closest("[data-vm-select]");
      if (checkbox && overview.contains(checkbox)) {
        syncVmOverviewSelection(overview);
      }
    });

    syncVmOverviewSelection(overview);
  });
};

const initVmOverviewAgentInfo = (root = document) => {
  root.querySelectorAll("[data-vm-overview][data-vm-agent-info-url]").forEach((overview) => {
    if (overview.dataset.agentInfoInitialized === "true") {
      return;
    }
    overview.dataset.agentInfoInitialized = "true";
    const agentInfoUrl = overview.dataset.vmAgentInfoUrl || "";
    if (!agentInfoUrl) {
      return;
    }

    const loadAgentInfo = async () => {
      try {
        const response = await fetch(new URL(agentInfoUrl, window.location.origin), {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        (data.guests || []).forEach((guest) => {
          const target = guest.target || "";
          const row = overview.querySelector(`[data-guest-target="${CSS.escape(target)}"]`);
          if (!row) {
            return;
          }
          const updates = [
            [row.querySelector("[data-agent-os-cell]"), guest.guest_os],
            [row.querySelector("[data-agent-status-cell]"), guest.agent],
          ];
          updates.forEach(([cell, value]) => {
            if (!cell || !value) {
              return;
            }
            cell.textContent = value;
            cell.dataset.sortValue = value;
          });
          renderIpCell(row.querySelector("[data-agent-ip-cell]"), guest.ip_label);
          const extraFilterText = [guest.guest_os, guest.ip_label, guest.agent].filter(Boolean).join(" ").toLowerCase();
          if (extraFilterText && !row.dataset.filterText.includes(extraFilterText)) {
            row.dataset.filterText = `${row.dataset.filterText} ${extraFilterText}`;
          }
        });
        applyStoredSortForOverview(overview);
      } catch (_error) {
        // Guest-agent enrichment is optional; the overview remains usable.
      }
    };

    overview.refreshVmAgentInfo = loadAgentInfo;
    loadAgentInfo();
  });
};

const initVmOverviewSnapshotInfo = (root = document) => {
  root.querySelectorAll("[data-vm-overview][data-vm-snapshot-info-url]").forEach((overview) => {
    if (overview.dataset.snapshotInfoInitialized === "true") {
      return;
    }
    overview.dataset.snapshotInfoInitialized = "true";
    const snapshotInfoUrl = overview.dataset.vmSnapshotInfoUrl || "";
    if (!snapshotInfoUrl) {
      return;
    }

    const loadSnapshotInfo = async () => {
      try {
        const response = await fetch(new URL(snapshotInfoUrl, window.location.origin), {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        (data.guests || []).forEach((guest) => {
          const target = guest.target || "";
          const row = overview.querySelector(`[data-guest-target="${CSS.escape(target)}"]`);
          const cell = row?.querySelector("[data-snapshot-status-cell]");
          if (!row || !cell) {
            return;
          }
          const value = guest.has_snapshot_label || (guest.has_snapshot ? "Yes" : "No");
          cell.textContent = value;
          cell.dataset.sortValue = value;
          const extraFilterText = String(value || "").toLowerCase();
          if (extraFilterText && !row.dataset.filterText.includes(extraFilterText)) {
            row.dataset.filterText = `${row.dataset.filterText} ${extraFilterText}`;
          }
        });
        applyStoredSortForOverview(overview);
      } catch (_error) {
        // Snapshot enrichment is optional; fallback values remain visible.
      }
    };

    overview.refreshVmSnapshotInfo = loadSnapshotInfo;
    loadSnapshotInfo();
  });
};

const updateVmRowStatus = (row, guest) => {
  const previousStatus = row.dataset.guestStatus || "";
  const status = guest.status || "";
  const stateLabel =
    guest.state_label ||
    (status === "running"
      ? "Powered On"
      : status === "stopped"
        ? "Powered Off"
        : status === "paused"
          ? "Suspended"
          : status === "hibernated"
            ? "Hibernated"
            : status
              ? status.charAt(0).toUpperCase() + status.slice(1)
              : "-");
  row.dataset.guestStatus = status;

  const stateCell = row.querySelector("[data-guest-state-cell]");
  if (stateCell) {
    stateCell.textContent = stateLabel;
    stateCell.dataset.sortValue = stateLabel;
  }

  const statusIcon = row.querySelector("[data-guest-status-icon]");
  if (statusIcon) {
    // The type icon (vm/container/template) is fixed; power state only toggles
    // the running-triangle / paused overlay.
    statusIcon.classList.toggle("running-icon", status === "running");
    statusIcon.classList.toggle("paused-icon", status === "paused");
    statusIcon.classList.toggle("hibernated-icon", status === "hibernated");
    statusIcon.title = status || "unknown";
  }

  // Lock badge (stale/active Proxmox config lock) — orthogonal to power state.
  const lock = guest.lock || "";
  row.dataset.guestLock = lock;
  const lockBadge = row.querySelector("[data-guest-lock-badge]");
  if (lockBadge) {
    lockBadge.hidden = !lock;
    lockBadge.title = lock ? `Locked: ${lock}` : "";
  }

  const activeBadge = row
    .closest("[data-vm-overview]")
    ?.querySelector(`[data-active-guest-status-badge][data-guest-target="${CSS.escape(guest.target || "")}"]`);
  if (activeBadge) {
    activeBadge.textContent = status || "-";
    activeBadge.classList.toggle("completed", status === "running");
  }

  if (status === "running" && previousStatus !== "running") {
    const overview = row.closest("[data-vm-overview]");
    if (overview && typeof overview.refreshVmAgentInfo === "function") {
      window.setTimeout(() => overview.refreshVmAgentInfo(), 1500);
    }
    document.querySelectorAll("[data-guest-agent-summary]").forEach((card) => {
      card.dataset.agentSummaryStatus = "running";
      delete card.dataset.agentSummaryInitialized;
    });
    window.setTimeout(() => initGuestAgentSummaries(document), 1500);
  }
};

const initVmStatusRefresh = (root = document) => {
  root.querySelectorAll("[data-vm-overview][data-vm-status-url]").forEach((overview) => {
    if (overview.dataset.statusRefreshInitialized === "true") {
      return;
    }
    overview.dataset.statusRefreshInitialized = "true";
    const statusUrl = overview.dataset.vmStatusUrl || "";
    if (!statusUrl) {
      return;
    }

    const refresh = async ({ force = false } = {}) => {
      if (!document.body.contains(overview) || (!force && document.hidden)) {
        return;
      }
      try {
        const response = await fetch(new URL(statusUrl, window.location.origin), {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        // Key by node-agnostic type:vmid so a guest that changed node (e.g. a
        // migration) is followed, not treated as gone and dropped from the list.
        const bareTarget = (value) => String(value || "").split("@")[0];
        const liveByBare = new Map();
        (data.guests || []).forEach((guest) => {
          liveByBare.set(bareTarget(guest.target), guest);
        });
        vmOverviewRows(overview).forEach((row) => {
          const guest = liveByBare.get(bareTarget(row.dataset.guestTarget || ""));
          if (!guest) {
            return;
          }
          const newTarget = guest.target || "";
          if (newTarget && newTarget !== row.dataset.guestTarget) {
            // Guest moved to another node — follow it in place.
            row.dataset.guestTarget = newTarget;
            const newNode = newTarget.split("@")[1] || "";
            const nodeCell = row.querySelector('[data-column="node"]');
            if (nodeCell && newNode) {
              nodeCell.textContent = newNode;
              nodeCell.dataset.sortValue = newNode;
            }
          }
          updateVmRowStatus(row, guest);
        });
        if (data.live_available) {
          vmOverviewRows(overview).forEach((row) => {
            const bare = bareTarget(row.dataset.guestTarget || "");
            if (bare && !liveByBare.has(bare)) {
              row.remove();
            }
          });
          syncVmOverviewSelection(overview);
        }
        applyStoredSortForOverview(overview);
        createIcons();
      } catch (_error) {
        // Status refresh is opportunistic; the current page stays usable if it fails.
      }
    };

    overview.refreshVmStatus = refresh;
    // Power state rarely changes on its own, so the steady poll is relaxed;
    // an action triggers a short burst (below) to reflect the change fast.
    overview.burstVmStatusRefresh = () => {
      [0, 1500, 4000, 8000].forEach((delay) => {
        window.setTimeout(() => refresh({ force: true }), delay);
      });
    };
    refresh();
    const intervalId = window.setInterval(refresh, 20000);
    registerPageCleanup(() => window.clearInterval(intervalId));
  });
};

const initGuestAgentSummaries = (root = document) => {
  root.querySelectorAll("[data-guest-agent-summary][data-agent-summary-url]").forEach((card) => {
    if (card.dataset.agentSummaryInitialized === "true") {
      return;
    }
    const url = card.dataset.agentSummaryUrl || "";
    const status = card.dataset.agentSummaryStatus || "";
    if (!url || status !== "running") {
      return;
    }
    card.dataset.agentSummaryInitialized = "true";

    const osName = card.querySelector("[data-agent-os-name]");
    const details = card.querySelector("[data-agent-details]");
    const statusBadge = card.querySelector("[data-agent-status-badge]");
    let attempts = 0;

    const renderRows = (rows) => {
      if (!details || !Array.isArray(rows)) {
        return;
      }
      details.querySelectorAll("[data-agent-dynamic-row]").forEach((row) => {
        row.remove();
      });
      rows.forEach((row) => {
        const wrapper = document.createElement("div");
        wrapper.dataset.agentDynamicRow = "true";
        const term = document.createElement("dt");
        term.textContent = row.label || "";
        const value = document.createElement("dd");
        if (row.label === "IP addresses") {
          String(row.value || "")
            .split("\n")
            .filter(Boolean)
            .forEach((line, index) => {
              if (index > 0) {
                const separator = document.createElement("br");
                separator.dataset.ipSeparator = "true";
                value.appendChild(separator);
              }
              const ip = document.createElement("span");
              ip.dataset.ipAddress = "true";
              ip.dataset.ipVersion = ipVersion(line);
              ip.textContent = line;
              value.appendChild(ip);
            });
        } else {
          String(row.value || "")
            .split("\n")
            .forEach((line, index) => {
              if (index > 0) {
                value.appendChild(document.createElement("br"));
              }
              value.appendChild(document.createTextNode(line));
            });
        }
        wrapper.append(term, value);
        details.insertBefore(wrapper, details.lastElementChild);
      });
      applyIpVersionStyle(document.documentElement.dataset.ipVersionStyle || "all");
    };

    const refresh = async () => {
      attempts += 1;
      try {
        const response = await fetch(new URL(url, window.location.origin), {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        if (osName && data.os_label) {
          osName.textContent = data.os_label;
        }
        renderRows(data.rows || []);
        if (statusBadge) {
          statusBadge.textContent = data.status_label || "Not running";
          statusBadge.classList.toggle("completed", Boolean(data.running));
        }
        if (!data.running && attempts < 3) {
          window.setTimeout(refresh, 5000);
        }
      } catch (_error) {
        if (attempts < 3) {
          window.setTimeout(refresh, 5000);
        }
      }
    };

    refresh();
  });
};

const vmActionAuditAction = (action) => {
  if (["start", "shutdown", "reboot", "stop", "reset", "suspend", "resume", "hibernate"].includes(action)) {
    return `guest.power.${action}`;
  }
  return (
    {
      snapshot: "guest.snapshot.create",
      snapshot_delete: "guest.snapshot.delete",
      snapshot_rollback: "guest.snapshot.rollback",
      delete_snapshots: "guest.snapshot.delete_all",
      template: "guest.template.convert",
      untemplate: "guest.template.revert",
      pool: "guest.pool.updated",
      migrate: "guest.migrate",
      clone: "guest.clone.create",
      clone_to_template: "guest.template.clone",
      tags: "guest.tags.updated",
      agent_enable: "guest.agent.enable",
      agent_disable: "guest.agent.disable",
      destroy: "guest.destroy",
      backup: "guest.backup.run",
      backup_delete: "guest.backup.delete",
    }[action] || `guest.${action}`
  );
};

const vmActionTaskName = (action) =>
  ({
    start: "Power on",
    shutdown: "Shut down guest",
    reboot: "Restart guest",
    stop: "Power off",
    reset: "Reset guest",
    suspend: "Suspend",
    resume: "Resume",
    hibernate: "Hibernate",
    snapshot: "Create snapshot",
    snapshot_delete: "Delete snapshot",
    snapshot_rollback: "Rollback snapshot",
    delete_snapshots: "Delete all snapshots",
    template: "Convert to template",
    untemplate: "Convert template to VM",
    pool: "Move to pool",
    migrate: "Migrate",
    clone: "Clone guest",
    clone_to_template: "Clone to template",
    tags: "Update tags",
    agent_enable: "Enable guest agent",
    agent_disable: "Disable guest agent",
    destroy: "Destroy guest",
    backup: "Backup",
    backup_delete: "Delete backup",
  })[action] || "VM/CT action";

const pendingVmTaskTarget = (rows) => {
  if (rows.length !== 1) {
    return {
      target: `${rows.length} selected guests`,
      target_guest: null,
      server: "-",
    };
  }
  const row = rows[0];
  const target = row.dataset.guestTarget || "";
  const [targetText, server = ""] = target.split("@");
  const [type = "", vmid = ""] = targetText.split(":");
  return {
    target: row.dataset.guestLabel || row.dataset.guestName || target || "Guest",
    target_guest: {
      type,
      vmid,
      name: row.dataset.guestName || row.dataset.guestLabel || "",
    },
    server: server || "-",
  };
};

const pendingVmTaskDetails = (action, fields) => {
  if (action === "snapshot") {
    return fields.snapshot_name || "-";
  }
  if (action === "tags") {
    return fields.tags_mode || "update";
  }
  if (action === "clone") {
    return fields.clone_name || fields.clone_newid || "-";
  }
  if (action === "pool") {
    return fields.pool_id || "No pool";
  }
  if (action === "migrate") {
    if (fields.migrate_kind === "storage") {
      return `→ ${fields.migrate_target_storage || "-"}`;
    }
    // Mirror the server-side detail exactly (incl. the remap suffix) so the
    // optimistic row reconciles with the loaded task instead of lingering.
    let detail = `→ ${fields.migrate_target_node || "-"}${fields.migrate_target_storage ? ` / ${fields.migrate_target_storage}` : ""}`;
    if (fields.migrate_net_remap) {
      try {
        const remap = JSON.parse(fields.migrate_net_remap);
        const parts = Object.entries(remap).map(([key, value]) => `${key}→${value}`);
        if (parts.length) {
          detail += ` (${parts.join(", ")})`;
        }
      } catch (_error) {
        /* ignore malformed remap */
      }
    }
    return detail;
  }
  if (action === "agent_enable") {
    return "enabled";
  }
  if (action === "agent_disable") {
    return "disabled";
  }
  return "-";
};

export {
  applyStoredSortForOverview,
  initGuestAgentSummaries,
  initVmOverviewAgentInfo,
  initVmOverviewSelection,
  initVmOverviewSnapshotInfo,
  initVmStatusRefresh,
  pendingVmTaskDetails,
  pendingVmTaskTarget,
  syncVmOverviewSelection,
  updateVmRowStatus,
  vmActionAuditAction,
  vmActionTaskName,
};
