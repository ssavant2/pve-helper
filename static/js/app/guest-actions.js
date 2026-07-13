import { selectedVmOverviewRows } from "./scheduling.js";
import {
  addPendingRecentTask,
  createIcons,
  escapeHtml,
  initTreeModules,
  refreshSidebarWidth,
  renderVIcons,
  runPageCleanup,
  softContentSelector,
  softStatusSelector,
  softTreeSelector,
  taskDateLabel,
  updatePendingRecentTask,
} from "./shell.js";
import { pendingVmTaskDetails, pendingVmTaskTarget, vmActionAuditAction, vmActionTaskName } from "./vm-overview.js";

let activeLabel = "";
let activeVmOverview = null;
let activeVmContextRows = [];
let activeTaskRow = null;
let navigationController = null;
let pageInitializer = () => {};

const setPageInitializer = (initializer) => {
  pageInitializer = typeof initializer === "function" ? initializer : () => {};
};

const updateVmRowsAgentState = (rows, enabled) => {
  const label = enabled ? "Enabled" : "Disabled";
  rows.forEach((row) => {
    row.dataset.guestAgentEnabled = enabled ? "true" : "false";
    const statusCell = row.querySelector("[data-agent-status-cell]");
    if (statusCell) {
      statusCell.textContent = label;
      statusCell.dataset.sortValue = label;
    }
  });
  document.querySelectorAll('[data-vm-detail-field="agent"] dd').forEach((field) => {
    field.textContent = label;
  });
};

const updateVmRowsTemplateState = (rows, isTemplate) => {
  rows.forEach((row) => {
    row.dataset.guestTemplate = isTemplate ? "true" : "false";
    if (row.dataset.guestType !== "vm") {
      return;
    }
    row.querySelectorAll("[data-vicon]").forEach((icon) => {
      icon.setAttribute("data-vicon", isTemplate ? "template" : "vm");
      delete icon.dataset.viconRendered;
    });
    const typeCell = row.querySelector('[data-column="type"]');
    if (typeCell) {
      const typeLabel = isTemplate ? "Template" : "VM";
      typeCell.textContent = typeLabel;
      typeCell.dataset.sortValue = typeLabel;
    }
  });
  renderVIcons(document);
};

const updateVmRowsPoolState = (rows, poolId) => {
  rows.forEach((row) => {
    row.dataset.guestPool = poolId || "";
    if (!row.classList.contains("active")) {
      return;
    }
    document.querySelectorAll('[data-vm-detail-field="pool"] dd').forEach((field) => {
      field.textContent = poolId || "No pool";
    });
  });
};

const createPendingVmTask = (action, fields, rows) => {
  const now = Date.now();
  const target = pendingVmTaskTarget(rows);
  return {
    id: `pending-vm-${now}-${Math.random().toString(36).slice(2)}`,
    kind: "guest",
    pending: true,
    pending_kind: "guest",
    action: vmActionAuditAction(action),
    name: vmActionTaskName(action),
    target: target.target,
    target_guest: target.target_guest,
    status: "Starting",
    status_class: "queued",
    details: pendingVmTaskDetails(action, fields),
    initiator: "-",
    queued_for: "-",
    started_at: taskDateLabel(new Date(now)),
    started_at_ms: now,
    finished_at: "-",
    finished_at_ms: 0,
    server: target.server,
    created_at_ms: now,
  };
};

// Optimistic pending task for a single-guest detail-page action form, built
// from the form's data attributes (data-action / data-guest-target / label).
const createPendingGuestFormTask = (form) => {
  const now = Date.now();
  const action = form.dataset.action || "";
  const target = form.dataset.guestTarget || "";
  const [targetText, server = ""] = target.split("@");
  const [type = "", vmid = ""] = targetText.split(":");
  return {
    id: `pending-guest-${now}-${Math.random().toString(36).slice(2)}`,
    kind: "guest",
    pending: true,
    pending_kind: "guest",
    action: vmActionAuditAction(action),
    name: vmActionTaskName(action),
    target: form.dataset.guestLabel || form.dataset.guestName || target || "Guest",
    target_guest: { type, vmid, name: form.dataset.guestName || "" },
    status: "Starting",
    status_class: "queued",
    details: "-",
    initiator: "-",
    queued_for: "-",
    started_at: taskDateLabel(new Date(now)),
    started_at_ms: now,
    finished_at: "-",
    finished_at_ms: 0,
    server: server || "-",
    created_at_ms: now,
  };
};

// General action flow: on submit, add a Recent Tasks row immediately, run the
// command via fetch (no full navigation), then update the row + refresh page
// state. Same principle as the Inventory bulk actions.
const initGuestActionForms = (root = document) => {
  root.querySelectorAll("form[data-guest-action-form]").forEach((form) => {
    if (form.dataset.actionFormInit === "true") {
      return;
    }
    form.dataset.actionFormInit = "true";
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (
        form.dataset.confirm &&
        !(await openConfirmDialog({
          title: "Confirm action",
          body: `<p>${escapeHtml(form.dataset.confirm)}</p>`,
          confirmLabel: "Confirm",
          danger: true,
        }))
      ) {
        return;
      }
      const pending = createPendingGuestFormTask(form);
      addPendingRecentTask(pending);
      let settled = false;
      window.setTimeout(() => {
        if (!settled) {
          updatePendingRecentTask({ id: pending.id, status: "Running", status_class: "running" });
        }
      }, 500);
      const fail = (message) => {
        settled = true;
        updatePendingRecentTask({
          id: pending.id,
          status: "Failed",
          status_class: "failed",
          details: message,
          finished_at: taskDateLabel(new Date()),
          finished_at_ms: Date.now(),
        });
        window.alert(message);
      };
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { Accept: "application/json", "X-Requested-With": "fetch" },
        });
        const payload = response.ok ? await response.json() : { ok: false, errors: [`HTTP ${response.status}`] };
        if (!payload.ok) {
          fail((payload.errors || ["Action failed."]).join("; "));
          return;
        }
        settled = true;
        updatePendingRecentTask({ id: pending.id, status: "Running", status_class: "running" });
        window.pveHelperRefreshRecentTasks?.();
        if (form.dataset.skipSoftRefresh !== "true") {
          // Refresh the page's own state (status badge, action menu) in place.
          loadSoftNavigation(new URL(window.location.href), { push: false });
          // The guest's live status (Proxmox cluster/resources) lags a few
          // seconds behind the action; refresh once more so the status badge/icon
          // catches up without a manual reload.
          const here = window.location.href;
          window.setTimeout(() => {
            if (window.location.href === here) {
              loadSoftNavigation(new URL(here), { push: false });
            }
          }, 4500);
        }
      } catch (_error) {
        fail("Network error");
      }
    });
  });
};

const initBackupRestoreForms = (root = document) => {
  root.querySelectorAll("[data-backup-restore]").forEach((page) => {
    if (page.dataset.restoreInit === "true") {
      return;
    }
    page.dataset.restoreInit = "true";
    const form = page.querySelector("[data-backup-restore-form]");
    const archiveSelect = page.querySelector("[data-restore-archive]");
    const nodeSelect = page.querySelector("[data-restore-node]");
    const storageSelect = page.querySelector("[data-restore-storage]");
    const overwrite = page.querySelector("[data-restore-overwrite]");
    const vmidInput = form?.querySelector('input[name="vmid"]');
    const confirmField = page.querySelector("[data-restore-confirm]");
    const confirmInput = confirmField?.querySelector("input");
    if (!form || !archiveSelect || !nodeSelect || !storageSelect) {
      return;
    }
    let storageOptions = {};
    try {
      const storageData = document.getElementById(page.dataset.storageOptionsId || "");
      storageOptions = JSON.parse(storageData?.textContent || "{}");
    } catch (_error) {
      storageOptions = {};
    }
    const selectedArchiveType = () => archiveSelect.selectedOptions[0]?.dataset.archiveType || "vm";
    const selectedArchiveVmid = () => archiveSelect.selectedOptions[0]?.dataset.sourceVmid || "";
    const syncTargetNodes = () => {
      const endpoint = archiveSelect.selectedOptions[0]?.dataset.endpoint || "";
      const current = nodeSelect.selectedOptions[0];
      Array.from(nodeSelect.options).forEach((option) => {
        option.hidden = option.dataset.endpoint !== endpoint;
        option.disabled = option.dataset.endpoint !== endpoint;
      });
      if (!current || current.disabled) {
        const firstAvailable = Array.from(nodeSelect.options).find((option) => !option.disabled);
        if (firstAvailable) {
          firstAvailable.selected = true;
        }
      }
    };
    const refreshStorages = () => {
      const node = nodeSelect.value || "";
      const options = storageOptions[node]?.[selectedArchiveType()] || [];
      const previous = storageSelect.value;
      storageSelect.innerHTML = "";
      if (!options.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "No compatible target storage";
        storageSelect.appendChild(option);
        return;
      }
      options.forEach((storage) => {
        const option = document.createElement("option");
        option.value = storage;
        option.textContent = storage;
        if (storage === previous) {
          option.selected = true;
        }
        storageSelect.appendChild(option);
      });
    };
    const syncOverwrite = () => {
      const enabled = Boolean(overwrite?.checked);
      if (vmidInput) {
        if (!vmidInput.dataset.restoreDefaultVmid) {
          vmidInput.dataset.restoreDefaultVmid = vmidInput.value;
        }
        vmidInput.value = enabled ? selectedArchiveVmid() : vmidInput.dataset.restoreDefaultVmid;
        vmidInput.readOnly = enabled;
        vmidInput.classList.toggle("restore-vmid-locked", enabled);
        vmidInput.setAttribute("aria-disabled", enabled ? "true" : "false");
      }
      if (confirmField) {
        confirmField.hidden = !enabled;
      }
      if (confirmInput) {
        confirmInput.required = enabled;
        if (!enabled) {
          confirmInput.value = "";
        }
      }
    };
    archiveSelect.addEventListener("change", () => {
      syncTargetNodes();
      refreshStorages();
      syncOverwrite();
    });
    nodeSelect.addEventListener("change", refreshStorages);
    overwrite?.addEventListener("change", syncOverwrite);
    syncTargetNodes();
    refreshStorages();
    syncOverwrite();
  });
};

const submitVmBulkAction = async (overview, action, fields = {}, targetRows = null, submitUrl = "") => {
  const form = overview.querySelector("[data-vm-bulk-form]");
  const actionInput = overview.querySelector("[data-vm-bulk-action]");
  const snapshotInput = overview.querySelector("[data-vm-bulk-snapshot-name]");
  const rows = targetRows || selectedVmOverviewRows(overview);
  if (!form || !actionInput || !snapshotInput || rows.length === 0) {
    return;
  }

  form.querySelectorAll("[data-vm-bulk-target]").forEach((input) => {
    input.remove();
  });
  form.querySelectorAll("[data-vm-bulk-extra]").forEach((input) => {
    input.remove();
  });
  rows.forEach((row) => {
    const target = row.dataset.guestTarget || "";
    if (!target) {
      return;
    }
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = "guest";
    input.value = target;
    input.dataset.vmBulkTarget = "true";
    form.appendChild(input);
  });
  actionInput.value = action;
  snapshotInput.value = fields.snapshot_name || "";
  Object.entries(fields).forEach(([name, value]) => {
    if (name === "snapshot_name") {
      return;
    }
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    input.dataset.vmBulkExtra = "true";
    form.appendChild(input);
  });
  const pendingTask = createPendingVmTask(action, fields, rows);
  addPendingRecentTask(pendingTask);
  let requestSettled = false;
  window.setTimeout(() => {
    if (requestSettled) {
      return;
    }
    updatePendingRecentTask({
      id: pendingTask.id,
      status: "Running",
      status_class: "running",
      details: pendingTask.details || "Accepted",
    });
  }, 500);
  try {
    const response = await fetch(submitUrl || form.action, {
      method: "POST",
      body: new FormData(form),
      headers: {
        Accept: "application/json",
        "X-Requested-With": "fetch",
      },
    });
    if (!response.ok) {
      requestSettled = true;
      updatePendingRecentTask({
        id: pendingTask.id,
        status: "Failed",
        status_class: "failed",
        details: `HTTP ${response.status}`,
        finished_at: taskDateLabel(new Date()),
        finished_at_ms: Date.now(),
      });
      window.alert(`VM/CT action failed: ${response.status}`);
      return;
    }
    const payload = await response.json();
    if (!payload.ok) {
      requestSettled = true;
      updatePendingRecentTask({
        id: pendingTask.id,
        status: "Failed",
        status_class: "failed",
        details: (payload.errors || ["VM/CT action failed."]).join("; "),
        finished_at: taskDateLabel(new Date()),
        finished_at_ms: Date.now(),
      });
      window.alert((payload.errors || ["VM/CT action failed."]).join("\n"));
    } else {
      requestSettled = true;
      // Bulk migrate can't reconcile one summary row against N per-guest server
      // tasks, so mark the summary accepted; the per-guest rows carry the real
      // running/completed/failed status.
      const bulkMigrate = action === "migrate" && rows.length > 1;
      if (
        action === "agent_enable" ||
        action === "agent_disable" ||
        action === "untemplate" ||
        action === "pool" ||
        action === "tags" ||
        bulkMigrate
      ) {
        const finishedAt = new Date();
        updatePendingRecentTask({
          id: pendingTask.id,
          status: bulkMigrate ? "Submitted" : "Completed",
          status_class: "completed",
          details: pendingTask.details || "Accepted",
          finished_at: taskDateLabel(finishedAt),
          finished_at_ms: finishedAt.getTime(),
        });
        if (action === "agent_enable" || action === "agent_disable") {
          updateVmRowsAgentState(rows, action === "agent_enable");
        }
        if (action === "untemplate") {
          updateVmRowsTemplateState(rows, false);
        }
        if (action === "pool") {
          updateVmRowsPoolState(rows, fields.pool_id || "");
        }
        window.pveHelperRefreshRecentTasks?.();
        if (action === "tags") {
          await loadSoftNavigation(new URL(window.location.href), { push: false });
        }
        if (bulkMigrate) {
          overview.burstVmStatusRefresh?.();
        }
        return;
      }
      updatePendingRecentTask({
        id: pendingTask.id,
        status: "Running",
        status_class: "running",
        details: pendingTask.details || "Accepted",
      });
      window.setTimeout(() => {
        window.pveHelperRefreshRecentTasks?.();
      }, 1200);
      // Burst-refresh the guests' power state so the change shows quickly
      // even though the steady status poll is relaxed.
      overview.burstVmStatusRefresh?.();
      return;
    }
    window.pveHelperRefreshRecentTasks?.();
  } catch (_error) {
    requestSettled = true;
    updatePendingRecentTask({
      id: pendingTask.id,
      status: "Failed",
      status_class: "failed",
      details: "Network error",
      finished_at: taskDateLabel(new Date()),
      finished_at_ms: Date.now(),
    });
    window.alert("VM/CT action failed: network error.");
  }
};

const clearVmContextHighlights = () => {
  document.querySelectorAll("[data-vm-overview-row].context-selected").forEach((row) => {
    row.classList.remove("context-selected");
  });
  activeVmContextRows = [];
};

const defaultSnapshotName = () => {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return `manual_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
};

const ensureVmActionDialog = () => {
  let dialog = document.querySelector("[data-vm-action-dialog]");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.className = "vm-action-dialog";
    dialog.dataset.vmActionDialog = "true";
    document.body.appendChild(dialog);
  }
  return dialog;
};

const selectedGuestSummary = (rows) => `${rows.length} selected guest${rows.length === 1 ? "" : "s"}`;

const guestRowIdentity = (row) => {
  const [target = ""] = String(row?.dataset.guestTarget || "").split("@");
  const [targetType = "", targetVmid = ""] = target.split(":");
  return {
    type: row?.dataset.guestType || targetType,
    vmid: row?.dataset.guestVmid || targetVmid,
  };
};

const openVmFormDialog = ({ title, summary, bodyHtml, submitLabel, submitClass = "primary-action", onSubmit }) => {
  const dialog = ensureVmActionDialog();
  dialog.innerHTML = `
      <form class="vm-action-dialog-form" method="dialog">
        <div class="vm-action-dialog-heading">
          <h2>${escapeHtml(title)}</h2>
          <button type="button" data-vm-dialog-close aria-label="Close">×</button>
        </div>
        <p class="panel-meta">${escapeHtml(summary)}</p>
        <div class="vm-action-dialog-body">${bodyHtml}</div>
        <p class="form-error" data-vm-dialog-error hidden></p>
        <div class="form-actions">
          <button class="${escapeHtml(submitClass)}" type="submit" data-vm-dialog-submit>${escapeHtml(submitLabel)}</button>
          <button class="secondary-action" type="button" data-vm-dialog-cancel>Cancel</button>
        </div>
      </form>
    `;
  const form = dialog.querySelector("form");
  const error = dialog.querySelector("[data-vm-dialog-error]");
  const close = () => dialog.close();
  dialog.querySelector("[data-vm-dialog-close]")?.addEventListener("click", close);
  dialog.querySelector("[data-vm-dialog-cancel]")?.addEventListener("click", close);
  form?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form) {
      return;
    }
    const message = onSubmit(new FormData(form));
    if (message) {
      if (error) {
        error.textContent = message;
        error.hidden = false;
      }
      return;
    }
    dialog.close();
  });
  if (typeof dialog.showModal === "function") {
    dialog.showModal();
  }
  return dialog;
};

// Shared confirm/consequence dialog. Returns a Promise<boolean> so it drops in
// for window.confirm inside async handlers: `if (!(await openConfirmDialog(...)))
// return;`. `body` is trusted HTML — escape any user/DB text before passing it.
const openConfirmDialog = ({ title = "Please confirm", body = "", confirmLabel = "Confirm", danger = false }) =>
  new Promise((resolve) => {
    const dialog = ensureVmActionDialog();
    let decided = false;
    dialog.innerHTML = `
        <div class="vm-action-dialog-form">
          <div class="vm-action-dialog-heading">
            <h2>${escapeHtml(title)}</h2>
            <button type="button" data-confirm-dismiss aria-label="Close">×</button>
          </div>
          <div class="vm-action-dialog-body">${body}</div>
          <div class="form-actions">
            <button class="primary-action${danger ? " danger-action" : ""}" type="button" data-confirm-yes>${escapeHtml(confirmLabel)}</button>
            <button class="secondary-action" type="button" data-confirm-no>Cancel</button>
          </div>
        </div>
      `;
    const finish = (result) => {
      if (decided) {
        return;
      }
      decided = true;
      resolve(result);
      dialog.close();
    };
    dialog.querySelector("[data-confirm-yes]")?.addEventListener("click", () => finish(true));
    dialog.querySelector("[data-confirm-no]")?.addEventListener("click", () => finish(false));
    dialog.querySelector("[data-confirm-dismiss]")?.addEventListener("click", () => finish(false));
    dialog.addEventListener("close", () => finish(false), { once: true });
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
  });

// Shared text-input dialog (Promise<string|null>) replacing window.prompt, so
// file naming flows can use the app dialog instead of a native browser prompt.
const openInputDialog = ({ title = "Enter a value", label = "", value = "", confirmLabel = "OK" }) =>
  new Promise((resolve) => {
    const dialog = ensureVmActionDialog();
    let decided = false;
    dialog.innerHTML = `
        <form class="vm-action-dialog-form" method="dialog">
          <div class="vm-action-dialog-heading">
            <h2>${escapeHtml(title)}</h2>
            <button type="button" data-input-dismiss aria-label="Close">×</button>
          </div>
          <label class="form-field">
            ${label ? `<span>${escapeHtml(label)}</span>` : ""}
            <input type="text" data-input-value autocomplete="off" value="${escapeHtml(value)}">
          </label>
          <div class="form-actions">
            <button class="primary-action" type="submit">${escapeHtml(confirmLabel)}</button>
            <button class="secondary-action" type="button" data-input-cancel>Cancel</button>
          </div>
        </form>
      `;
    const field = dialog.querySelector("[data-input-value]");
    const finish = (result) => {
      if (decided) {
        return;
      }
      decided = true;
      resolve(result);
      dialog.close();
    };
    dialog.querySelector("form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      finish((field?.value ?? "").trim() || null);
    });
    dialog.querySelector("[data-input-cancel]")?.addEventListener("click", () => finish(null));
    dialog.querySelector("[data-input-dismiss]")?.addEventListener("click", () => finish(null));
    dialog.addEventListener("close", () => finish(null), { once: true });
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    }
    field?.focus();
  });

// Issue an ungraceful hard stop for one guest straight from the taskbar (used
// by the force-stop follow-up on a timed-out graceful shutdown). POSTs the same
// bulk "stop" action the overview uses, with an optimistic pending task.
const forceStopGuest = async (target, label) => {
  const taskbar = document.querySelector("[data-recent-tasks]");
  const bulkUrl = taskbar?.dataset.vmsBulkActionUrl || "";
  const csrf = taskbar?.dataset.csrfToken || "";
  if (!bulkUrl || !target) {
    return;
  }
  // Build the pending task exactly like the overview's stop action so it
  // reconciles with (and is replaced by) the real "Power off" server task
  // instead of lingering as a duplicate.
  const now = Date.now();
  const [targetText, server = ""] = target.split("@");
  const [type = "", vmid = ""] = targetText.split(":");
  const pending = {
    id: `pending-forcestop-${now}-${Math.random().toString(36).slice(2)}`,
    kind: "guest",
    pending: true,
    pending_kind: "guest",
    action: vmActionAuditAction("stop"),
    name: vmActionTaskName("stop"),
    target: label || target,
    target_guest: { type, vmid, name: label || "" },
    status: "Starting",
    status_class: "queued",
    details: "Force stop",
    initiator: "-",
    queued_for: "-",
    started_at: taskDateLabel(new Date(now)),
    started_at_ms: now,
    finished_at: "-",
    finished_at_ms: 0,
    server,
    created_at_ms: now,
  };
  addPendingRecentTask(pending);
  const fail = (message) => {
    updatePendingRecentTask({
      id: pending.id,
      status: "Failed",
      status_class: "failed",
      details: message,
      finished_at: taskDateLabel(new Date()),
      finished_at_ms: Date.now(),
    });
  };
  try {
    const response = await fetch(new URL(bulkUrl, window.location.origin), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf,
        "X-Requested-With": "fetch",
      },
      body: new URLSearchParams({ bulk_action: "stop", guest: target }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload.ok) {
      fail((payload.errors || [`Force stop failed (HTTP ${response.status})`]).join("; "));
      return;
    }
    updatePendingRecentTask({ id: pending.id, status: "Running", status_class: "running", details: "Force stop" });
    // Pull the real "Power off" task in now so it reconciles the pending row
    // and the resolved shutdown question stops pulsing promptly.
    if (typeof window.pveHelperRefreshRecentTasks === "function") {
      window.pveHelperRefreshRecentTasks();
    }
  } catch (_error) {
    fail("Network error");
  }
};

// Mark a task's question as answered so it stops pulsing/pinning. Any close of
// the dialog counts as answering it (acted on it, or actively chose to ignore).
const dismissTaskQuestion = async (taskId) => {
  const taskbar = document.querySelector("[data-recent-tasks]");
  const url = taskbar?.dataset.dismissQuestionUrl || "";
  const csrf = taskbar?.dataset.csrfToken || "";
  if (!url || !taskId) {
    return;
  }
  try {
    await fetch(new URL(url, window.location.origin), {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-CSRFToken": csrf,
        "X-Requested-With": "fetch",
      },
      body: new URLSearchParams({ task_id: taskId }),
    });
  } catch (_error) {
    // Best effort; the next poll still reflects server state.
  }
  if (typeof window.pveHelperRefreshRecentTasks === "function") {
    window.pveHelperRefreshRecentTasks();
  }
};

const openForceStopDialog = (target, label, taskId) => {
  const dialog = openVmFormDialog({
    title: "Shutdown timed out",
    summary: label || target,
    submitLabel: "Force stop",
    submitClass: "primary-action danger-action",
    bodyHtml: `
        <p>The graceful shutdown of <strong>${escapeHtml(label || target)}</strong> timed out — the guest did not respond to the ACPI power signal (no <code>acpid</code> or QEMU guest agent running), so it is <strong>still running</strong>.</p>
        <p><strong>Force stop</strong> is an ungraceful hard power-off, like pulling the plug: unsaved data inside the guest may be lost.</p>
      `,
    onSubmit: () => {
      forceStopGuest(target, label);
      return "";
    },
  });
  // Whether the user force-stops or cancels/dismisses, the question is answered.
  if (taskId) {
    dialog.addEventListener("close", () => dismissTaskQuestion(taskId), { once: true });
  }
};

const openSnapshotDialog = (overview, rows) => {
  openVmFormDialog({
    title: "Take Snapshot",
    summary: selectedGuestSummary(rows),
    submitLabel: "Take Snapshot",
    bodyHtml: `
        <label class="form-field">
          <span>Snapshot name</span>
          <input type="text" name="snapshot_name" value="${escapeHtml(defaultSnapshotName())}" autocomplete="off" required>
        </label>
      `,
    onSubmit: (formData) => {
      const snapshotName = String(formData.get("snapshot_name") || "").trim();
      if (!/^[A-Za-z][A-Za-z0-9_-]*$/.test(snapshotName)) {
        return "Snapshot names must start with a letter and can then contain letters, digits, _ and -.";
      }
      submitVmBulkAction(overview, "snapshot", { snapshot_name: snapshotName }, rows);
      return "";
    },
  });
};

const openBackupDialog = (overview, rows) => {
  const optionsUrl = rows[0]?.dataset.backupOptionsUrl || "";
  const dialog = openVmFormDialog({
    title: "Back Up Now",
    summary: selectedGuestSummary(rows),
    submitLabel: "Start Backup",
    bodyHtml: `
        <label class="form-field"><span>Storage</span><select name="storage" disabled><option value="">Loading backup storage…</option></select></label>
        <label class="form-field"><span>Mode</span><select name="mode"><option value="snapshot">Snapshot - no downtime</option><option value="suspend">Suspend - brief pause</option><option value="stop">Stop - offline backup</option></select></label>
        <label class="form-field"><span>Compression</span><select name="compress"><option value="zstd">ZSTD - fast and good</option><option value="gzip">GZIP - best compatibility</option><option value="lzo">LZO - low CPU use</option><option value="0">None</option></select></label>
        <label class="form-field"><span>Notifications</span><select name="notification_mode"><option value="auto">Use global settings</option><option value="notification-system">Proxmox notification system</option><option value="legacy-sendmail">Legacy sendmail</option></select></label>
        <label class="form-field"><span>Notes</span><input name="notes_template" value="{{guestname}}"></label>
        <label class="form-field-inline"><input type="checkbox" name="protected"><span>Protect archive from automatic pruning</span></label>
      `,
    onSubmit: (formData) => {
      const storage = String(formData.get("storage") || "").trim();
      if (!storage) {
        return "Choose a backup storage.";
      }
      submitVmBulkAction(
        overview,
        "backup",
        {
          storage,
          mode: String(formData.get("mode") || "snapshot"),
          compress: String(formData.get("compress") || "zstd"),
          notification_mode: String(formData.get("notification_mode") || "auto"),
          notes_template: String(formData.get("notes_template") || "").trim(),
          protected: formData.get("protected") ? "1" : "0",
        },
        rows
      );
      return "";
    },
  });
  const submit = dialog?.querySelector("[data-vm-dialog-submit]");
  const select = dialog?.querySelector("[name='storage']");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submit) {
    submit.disabled = true;
  }
  fetch(optionsUrl, { headers: { Accept: "application/json", "X-Requested-With": "fetch" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }
      return response.json();
    })
    .then((data) => {
      const storages = data.storages || [];
      if (!select) {
        return;
      }
      select.innerHTML = "";
      storages.forEach((storage) => {
        const option = document.createElement("option");
        option.value = storage.id;
        option.textContent = storage.label || storage.id;
        select.appendChild(option);
      });
      select.disabled = !storages.length;
      if (submit) {
        submit.disabled = !storages.length;
      }
      if (!storages.length && error) {
        error.textContent = data.error || "No active backup storage is available on this guest's node.";
        error.hidden = false;
      }
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load backup storage.";
        error.hidden = false;
      }
    });
};

const guestRowTags = (row) => {
  const metadataTags = String(row?.dataset.guestTags || "")
    .split(";")
    .map((tag) => tag.trim())
    .filter(Boolean);
  const renderedTags = Array.from(
    row?.querySelectorAll("[data-user-tag]") || [],
    (chip) => chip.dataset.userTag
  ).filter(Boolean);
  return [...new Set([...metadataTags, ...renderedTags])];
};

const availableGuestTags = (overview) => {
  const options = overview.querySelector("#vm-overview-tag-options");
  let registered = [];
  try {
    registered = JSON.parse(options?.textContent || "[]");
  } catch (_error) {
    // Tags rendered on the rows remain usable if registry metadata is stale.
  }
  const rendered = Array.from(overview.querySelectorAll("[data-vm-overview-row]")).flatMap(guestRowTags);
  return [...new Set([...registered, ...rendered])].sort((left, right) => left.localeCompare(right));
};

const tagChoicesForRows = (overview, rows, mode) => {
  if (mode === "remove") {
    return [...new Set(rows.flatMap(guestRowTags))].sort((left, right) => left.localeCompare(right));
  }
  return availableGuestTags(overview);
};

const openTagsDialog = (overview, rows, mode) => {
  const adding = mode === "add";
  const choices = tagChoicesForRows(overview, rows, mode);
  const options = choices.map((tag) => `<option value="${escapeHtml(tag)}">${escapeHtml(tag)}</option>`).join("");
  openVmFormDialog({
    title: adding ? "Add Tags" : "Remove Tags",
    summary: selectedGuestSummary(rows),
    submitLabel: adding ? "Add" : "Remove",
    bodyHtml: `
        <label class="form-field">
          <span>${adding ? "Existing tag" : "Assigned tag"}</span>
          <select name="tags_value" ${choices.length ? "" : "disabled"}>
            ${options || `<option value="">${adding ? "No user tags are registered" : "No user tags are assigned"}</option>`}
          </select>
        </label>
      `,
    onSubmit: (formData) => {
      const tag = String(formData.get("tags_value") || "").trim();
      if (!tag) {
        return adding ? "Choose a tag to add." : "Choose a tag to remove.";
      }
      submitVmBulkAction(overview, "tags", { tags_mode: mode, tags_value: tag }, rows);
      return "";
    },
  });
};

const openPoolDialog = (overview, rows) => {
  const row = rows[0];
  const optionsUrl = row?.dataset.poolOptionsUrl || "";
  const dialog = openVmFormDialog({
    title: "Move to Pool",
    summary: selectedGuestSummary(rows),
    submitLabel: "Move",
    bodyHtml: `
        <label class="form-field">
          <span>Pool</span>
          <select name="pool_id" disabled>
            <option value="">Loading pools...</option>
          </select>
        </label>
        <p class="form-hint" data-pool-current hidden></p>
      `,
    onSubmit: (formData) => {
      submitVmBulkAction(overview, "pool", { pool_id: String(formData.get("pool_id") || "") }, rows);
      return "";
    },
  });
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const poolSelect = dialog?.querySelector("[name='pool_id']");
  const currentHint = dialog?.querySelector("[data-pool-current]");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve pool options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load pools.");
      }
      return response.json();
    })
    .then((data) => {
      if (!poolSelect) {
        return;
      }
      poolSelect.innerHTML = "";
      const noPool = document.createElement("option");
      noPool.value = "";
      noPool.textContent = "No pool";
      poolSelect.appendChild(noPool);
      const pools = Array.isArray(data.pools) ? data.pools : [];
      pools.forEach((pool) => {
        const option = document.createElement("option");
        option.value = pool.id || "";
        option.textContent = pool.label || pool.id || "";
        option.selected = option.value === (data.current_pool || "");
        poolSelect.appendChild(option);
      });
      if (currentHint) {
        if (Array.isArray(data.multiple_memberships) && data.multiple_memberships.length) {
          currentHint.textContent = `Warning: this guest appears in multiple pools (${data.multiple_memberships.join(", ")}). The move will be blocked until that is resolved.`;
          currentHint.hidden = false;
        } else if (data.current_pool) {
          currentHint.textContent = `Current pool: ${data.current_pool}`;
          currentHint.hidden = false;
        }
      }
      poolSelect.disabled = false;
      if (submitButton) {
        submitButton.disabled = Boolean(Array.isArray(data.multiple_memberships) && data.multiple_memberships.length);
      }
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load pools.";
        error.hidden = false;
      }
    });
};

const formatMigrateBytes = (bytes) => {
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
};

const openBulkMigrateDialog = (overview, rows) => {
  // Bulk applies one choice to all selected guests. Per-guest NICs/CPU differ,
  // so there's no network mapping or per-guest CPU annotation here — NICs move
  // as-is and each guest's preflight/outcome appears per-row in Recent Tasks.
  // storage = move every guest's disks to the target storage.
  const optionsUrl = rows[0]?.dataset.migrateOptionsUrl || "";
  let optionsData = null;
  const dialog = openVmFormDialog({
    title: "Migrate",
    summary: `${rows.length} selected guests`,
    submitLabel: "Migrate",
    bodyHtml: `
        <p class="form-hint">Applies to all selected guests. NICs move as-is; per-guest results (including any that can't migrate) appear in Recent Tasks.</p>
        <fieldset class="form-field vm-migrate-kind">
          <span>What to migrate</span>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="host" checked> Change host</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="storage"> Change storage (all disks)</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="both"> Change host and storage</label>
        </fieldset>
        <label class="form-field" data-migrate-node-field>
          <span>Target node</span>
          <select name="migrate_target_node" disabled><option value="">Loading nodes…</option></select>
        </label>
        <label class="form-field" data-migrate-storage-field hidden>
          <span>Target storage</span>
          <select name="migrate_target_storage"></select>
        </label>
        <p class="form-hint migrate-warn" data-migrate-warn hidden></p>
      `,
    onSubmit: (formData) => {
      const kind = String(formData.get("migrate_kind") || "");
      const targetNode = String(formData.get("migrate_target_node") || "").trim();
      const targetStorage = String(formData.get("migrate_target_storage") || "").trim();
      if ((kind === "host" || kind === "both") && !targetNode) {
        return "Choose a target node.";
      }
      if ((kind === "storage" || kind === "both") && !targetStorage) {
        return "Choose a target storage.";
      }
      submitVmBulkAction(
        overview,
        "migrate",
        {
          migrate_kind: kind,
          migrate_target_node: kind === "storage" ? "" : targetNode,
          migrate_target_storage: kind === "host" ? "" : targetStorage,
        },
        rows
      );
      return "";
    },
  });
  const nodeField = dialog?.querySelector("[data-migrate-node-field]");
  const storageField = dialog?.querySelector("[data-migrate-storage-field]");
  const nodeSelect = dialog?.querySelector("[name='migrate_target_node']");
  const storageSelect = dialog?.querySelector("[name='migrate_target_storage']");
  const warnBox = dialog?.querySelector("[data-migrate-warn]");
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  let perGuestNics = null;
  const setShown = (field, shown) => {
    if (field) {
      field.style.display = shown ? "" : "none";
    }
  };
  const currentKind = () => dialog?.querySelector("[name='migrate_kind']:checked")?.value || "host";
  // Bulk NICs move as-is, so warn (and name the guests) when a selected guest's
  // bridge isn't realized on the target node — it would land without a network.
  const updateWarnings = () => {
    if (!warnBox) {
      return;
    }
    const kind = currentKind();
    const node = nodeSelect?.value || "";
    if (kind === "storage" || !node || !perGuestNics || !optionsData) {
      warnBox.hidden = true;
      return;
    }
    const available = optionsData.bridges_by_node?.[node] || [];
    const affected = [];
    perGuestNics.forEach((guest) => {
      const missing = [...new Set((guest.bridges || []).filter((bridge) => !available.includes(bridge)))];
      if (missing.length) {
        affected.push(`${guest.label} (${missing.join(", ")})`);
      }
    });
    if (affected.length) {
      warnBox.textContent = `⚠ Bridges not on ${node} — these guests will land without a network: ${affected.join("; ")}.`;
      warnBox.hidden = false;
    } else {
      warnBox.hidden = true;
    }
  };
  const fillStorage = (nodeName) => {
    if (!storageSelect) {
      return;
    }
    const ids = optionsData?.storages_by_node?.[nodeName] || [];
    storageSelect.innerHTML = "";
    if (!ids.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No compatible storage";
      storageSelect.appendChild(option);
    } else {
      ids.forEach((id) => {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = id;
        storageSelect.appendChild(option);
      });
    }
  };
  const syncFields = () => {
    const kind = currentKind();
    setShown(nodeField, kind !== "storage");
    setShown(storageField, kind !== "host");
    // storage: each guest moves on its own node — offer the source-node storages
    // (rows[0]'s node); both: offer the target node's storages.
    if (kind === "storage") {
      fillStorage(optionsData?.current_node || "");
    } else if (kind === "both") {
      fillStorage(nodeSelect?.value || "");
    }
    updateWarnings();
  };
  dialog?.querySelectorAll("[name='migrate_kind']").forEach((radio) => {
    radio.addEventListener("change", syncFields);
  });
  nodeSelect?.addEventListener("change", syncFields);
  // Fetch each selected guest's NIC bridges once for the missing-bridge warning.
  const nicsUrl = overview?.dataset.vmMigrateNicsUrl || "";
  if (nicsUrl) {
    const csrf = overview.querySelector("[data-vm-bulk-form] [name=csrfmiddlewaretoken]")?.value || "";
    const body = new URLSearchParams();
    rows.forEach((row) => {
      body.append("guest", row.dataset.guestTarget || "");
    });
    fetch(new URL(nicsUrl, window.location.origin), {
      method: "POST",
      headers: { "X-CSRFToken": csrf, "X-Requested-With": "fetch" },
      body,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        perGuestNics = Array.isArray(data?.guests) ? data.guests : [];
        updateWarnings();
      })
      .catch(() => {
        /* the NIC warning is best-effort */
      });
  }
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve migrate options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load migrate options.");
      }
      return response.json();
    })
    .then((data) => {
      optionsData = data;
      if (nodeSelect) {
        const online = (Array.isArray(data.nodes) ? data.nodes : []).filter((node) => node.online);
        nodeSelect.innerHTML = "";
        if (!online.length) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "No other online cluster node";
          nodeSelect.appendChild(option);
          nodeSelect.disabled = true;
        } else {
          online.forEach((node) => {
            const option = document.createElement("option");
            option.value = node.node;
            option.textContent = node.node;
            nodeSelect.appendChild(option);
          });
          nodeSelect.disabled = false;
        }
      }
      if (submitButton) {
        submitButton.disabled = false;
      }
      syncFields();
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load migrate options.";
        error.hidden = false;
      }
    });
};

const openMigrateDialog = (overview, rows) => {
  if (rows.length > 1) {
    openBulkMigrateDialog(overview, rows);
    return;
  }
  const row = rows[0];
  const label = row?.dataset.guestLabel || "guest";
  const optionsUrl = row?.dataset.migrateOptionsUrl || "";
  let optionsData = null;
  const dialog = openVmFormDialog({
    title: "Migrate",
    summary: label,
    submitLabel: "Migrate",
    bodyHtml: `
        <fieldset class="form-field vm-migrate-kind">
          <span>What to migrate</span>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="host" checked> Change host</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="storage"> Change storage</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="both"> Change host and storage</label>
        </fieldset>
        <label class="form-field" data-migrate-node-field>
          <span>Target node</span>
          <select name="migrate_target_node" disabled><option value="">Loading nodes…</option></select>
        </label>
        <label class="form-field" data-migrate-storage-field hidden>
          <span>Target storage</span>
          <select name="migrate_target_storage"></select>
          <span class="form-hint" data-migrate-storage-hint hidden>All of the guest's disks move to this storage.</span>
        </label>
        <div class="form-field migrate-net-check" data-migrate-net-field hidden>
          <span>Network mapping</span>
          <label class="form-field-inline"><input type="checkbox" data-migrate-net-ignore> Ignore network mapping (migrate NICs as-is)</label>
          <div data-migrate-net-body></div>
        </div>
        <p class="form-hint" data-migrate-hint hidden></p>
        <p class="form-hint migrate-warn" data-migrate-warn hidden></p>
      `,
    onSubmit: (formData) => {
      const kind = String(formData.get("migrate_kind") || "");
      const targetNode = String(formData.get("migrate_target_node") || "").trim();
      const targetStorage = String(formData.get("migrate_target_storage") || "").trim();
      if (kind === "host" || kind === "both") {
        if (!targetNode) {
          return "Choose a target node.";
        }
        const opt = nodeSelect?.selectedOptions?.[0];
        if (opt?.dataset.allowed === "false") {
          return opt.dataset.reason
            ? `That node can't be a migration target: ${opt.dataset.reason}.`
            : "That node can't be a migration target.";
        }
        if (opt?.dataset.cpuOk === "false") {
          return opt.dataset.cpuReason ? `${opt.dataset.cpuReason}.` : "The target host can't run this VM's CPU model.";
        }
      }
      if ((kind === "both" || kind === "storage") && !targetStorage) {
        return "Choose a target storage.";
      }
      const fields = {
        migrate_kind: kind,
        migrate_target_node: kind === "storage" ? "" : targetNode,
        migrate_target_storage: kind === "host" ? "" : targetStorage,
      };
      if (kind !== "storage") {
        const remap = collectRemap();
        if (Object.keys(remap).length) {
          fields.migrate_net_remap = JSON.stringify(remap);
        }
      }
      submitVmBulkAction(overview, "migrate", fields, rows);
      return "";
    },
  });
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  const nodeField = dialog?.querySelector("[data-migrate-node-field]");
  const storageField = dialog?.querySelector("[data-migrate-storage-field]");
  const storageHint = dialog?.querySelector("[data-migrate-storage-hint]");
  const nodeSelect = dialog?.querySelector("[name='migrate_target_node']");
  const storageSelect = dialog?.querySelector("[name='migrate_target_storage']");
  const netField = dialog?.querySelector("[data-migrate-net-field]");
  const netBody = dialog?.querySelector("[data-migrate-net-body]");
  const netIgnore = dialog?.querySelector("[data-migrate-net-ignore]");
  const hint = dialog?.querySelector("[data-migrate-hint]");
  const warnBox = dialog?.querySelector("[data-migrate-warn]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  const currentKind = () => dialog?.querySelector("[name='migrate_kind']:checked")?.value || "host";
  const fillStorage = (nodeName) => {
    if (!storageSelect) {
      return;
    }
    const ids = optionsData?.storages_by_node?.[nodeName] || [];
    storageSelect.innerHTML = "";
    if (!ids.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No compatible storage";
      storageSelect.appendChild(option);
    } else {
      ids.forEach((id) => {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = id;
        storageSelect.appendChild(option);
      });
    }
  };
  const updateHint = () => {
    if (!hint) {
      return;
    }
    const kind = currentKind();
    const opt = nodeSelect?.selectedOptions?.[0];
    if ((kind === "host" || kind === "both") && opt?.dataset.allowed === "false") {
      hint.textContent = opt.dataset.reason
        ? `Blocked target: ${opt.dataset.reason}.`
        : "This node can't be a migration target.";
      hint.hidden = false;
      return;
    }
    if ((kind === "host" || kind === "both") && optionsData?.running) {
      hint.textContent =
        optionsData.object_type === "vm"
          ? "Running VM → live (online) migration."
          : "Running container → restart migration (brief downtime).";
      hint.hidden = false;
      return;
    }
    hint.hidden = true;
  };
  // Toggle via inline display: the dialog's .form-field is `display: grid`,
  // which would override the [hidden] attribute and leave fields visible.
  const setShown = (field, shown) => {
    if (field) {
      field.style.display = shown ? "" : "none";
    }
  };
  // Only send a remap for NICs whose chosen bridge differs from the current
  // one; skip entirely when "ignore network mapping" is ticked.
  const collectRemap = () => {
    const out = {};
    if (netIgnore?.checked) {
      return out;
    }
    netBody?.querySelectorAll("[data-remap-net]").forEach((select) => {
      const value = String(select.value || "").trim();
      if (value && value !== select.dataset.current) {
        out[select.dataset.remapNet] = value;
      }
    });
    return out;
  };
  // Proxmox has no per-host port groups: a NIC's bridge name must exist on the
  // target node. Only surface a NIC when there's a decision to make — its
  // bridge is missing on the target, or it's on a default `vmbrN` bridge (which
  // exists everywhere, so picking a specific net is worthwhile). A custom
  // bridge already present on the target (e.g. server10→server10) needs no
  // prompt. Choosing a different bridge edits the guest config (permanent,
  // cluster-wide — see the backend).
  const isDefaultBridge = (bridge) => /^vmbr\d+$/.test(bridge || "");
  const renderNetCheck = () => {
    if (!netField || !netBody) {
      return;
    }
    const kind = currentKind();
    const node = nodeSelect?.value || "";
    const nics = Array.isArray(optionsData?.guest_nics) ? optionsData.guest_nics : [];
    const available = optionsData?.bridges_by_node?.[node] || [];
    const relevant =
      kind === "storage" || !node
        ? []
        : nics.filter((nic) => !available.includes(nic.bridge) || isDefaultBridge(nic.bridge));
    if (!relevant.length) {
      setShown(netField, false);
      netBody.innerHTML = "";
      return;
    }
    setShown(netField, true);
    if (netIgnore?.checked) {
      netBody.innerHTML = "";
      return;
    }
    // Selectable = bridges realized on the node + all cluster SDN vnets (which
    // are cluster-scoped). "present" (for the warning/default) stays realized-only.
    const vnets = Array.isArray(optionsData?.sdn_vnets) ? optionsData.sdn_vnets : [];
    const selectable = Array.from(new Set([...available, ...vnets])).sort();
    netBody.innerHTML = relevant
      .map((nic) => {
        const present = available.includes(nic.bridge);
        const keepOption = present
          ? `<option value="${escapeHtml(nic.bridge)}" selected>${escapeHtml(nic.bridge)} (unchanged)</option>`
          : `<option value="" selected>Keep “${escapeHtml(nic.bridge)}” — missing on ${escapeHtml(node)}</option>`;
        const options = keepOption.concat(
          selectable
            .filter((bridge) => bridge !== nic.bridge)
            .map((bridge) => `<option value="${escapeHtml(bridge)}">${escapeHtml(bridge)}</option>`)
            .join("")
        );
        const warn = present
          ? ""
          : `<span class="form-hint">⚠ bridge “${escapeHtml(nic.bridge)}” is not on ${escapeHtml(node)} — the NIC will have no network unless remapped.</span>`;
        return `<div class="migrate-net-warn">
              <label class="form-field-inline"><span>${escapeHtml(nic.key)}</span><select data-remap-net="${escapeHtml(nic.key)}" data-current="${escapeHtml(nic.bridge)}">${options}</select></label>
              ${warn}
            </div>`;
      })
      .join("");
    setShown(netField, true);
  };
  netIgnore?.addEventListener("change", renderNetCheck);
  // Preflight cautions Proxmox itself doesn't check: passthrough/local
  // resources that block migration, cpu=host (non-portable for live
  // migration), and a target host whose CPU can't run the VM's model (EVC-lite).
  const updateWarnings = () => {
    if (!warnBox) {
      return;
    }
    const kind = currentKind();
    const lines = [];
    if (kind !== "storage") {
      const local = Array.isArray(optionsData?.local_resources) ? optionsData.local_resources : [];
      if (local.length) {
        lines.push(`⚠ Local/passthrough resources may block migration: ${local.join(", ")}.`);
      }
      const opt = nodeSelect?.selectedOptions?.[0];
      if (opt?.dataset.cpuOk === "false") {
        lines.push(`⚠ ${opt.dataset.cpuReason || "The target host can't run this VM's CPU model."}.`);
      }
      // cpu=host is only risky for a live migration between differing hosts;
      // silent when the guest is stopped (offline) or the CPUs match.
      if ((optionsData?.guest_cpu || "") === "host" && optionsData?.running && opt?.dataset.hostCpuMatch === "false") {
        lines.push(
          `⚠ cpu=host and ${opt.dataset.hostCpuReason || "the target host CPU differs"} — live migration will likely crash the guest (pin a CPU model, or migrate while stopped).`
        );
      }
    }
    // Target-storage capacity: warn when free space looks short of the guest's
    // provisioned disks (actual usage may be less for thin/sparse volumes).
    if (kind === "storage" || kind === "both") {
      const storageNode = kind === "storage" ? optionsData?.current_node || "" : nodeSelect?.value || "";
      const storageId = storageSelect?.value || "";
      const need = Number(optionsData?.guest_disk_bytes || 0);
      const free = optionsData?.storage_free_by_node?.[storageNode]?.[storageId];
      if (need > 0 && typeof free === "number" && free < need) {
        lines.push(
          `⚠ Target storage ${storageId} has ${formatMigrateBytes(free)} free, but the guest's disks are provisioned at ${formatMigrateBytes(need)} (thin/sparse disks may still fit).`
        );
      }
    }
    if (lines.length) {
      warnBox.innerHTML = lines.map((line) => escapeHtml(line)).join("<br>");
      warnBox.hidden = false;
    } else {
      warnBox.hidden = true;
    }
  };
  const syncFields = () => {
    const kind = currentKind();
    setShown(nodeField, kind !== "storage");
    setShown(storageField, kind !== "host");
    if (storageHint) {
      storageHint.hidden = kind !== "storage";
    }
    if (kind === "storage") {
      fillStorage(optionsData?.current_node || "");
    } else if (kind === "both") {
      fillStorage(nodeSelect?.value || "");
    }
    updateHint();
    renderNetCheck();
    updateWarnings();
  };
  dialog?.querySelectorAll("[name='migrate_kind']").forEach((radio) => {
    radio.addEventListener("change", syncFields);
  });
  nodeSelect?.addEventListener("change", syncFields);
  // Storage choice only affects the capacity warning — don't re-run syncFields
  // (which would repopulate and reset the select).
  storageSelect?.addEventListener("change", updateWarnings);
  syncFields();
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve migrate options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load migrate options.");
      }
      return response.json();
    })
    .then((data) => {
      optionsData = data;
      if (nodeSelect) {
        nodeSelect.innerHTML = "";
        const nodes = Array.isArray(data.nodes) ? data.nodes : [];
        if (!nodes.length) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "No other cluster node";
          nodeSelect.appendChild(option);
          nodeSelect.disabled = true;
          // Only storage-only migration is possible without a second node.
          dialog?.querySelectorAll("[name='migrate_kind']").forEach((radio) => {
            if (radio.value !== "storage") {
              radio.disabled = true;
            }
          });
          const storageRadio = dialog?.querySelector("[name='migrate_kind'][value='storage']");
          if (storageRadio) {
            storageRadio.checked = true;
          }
        } else {
          nodes.forEach((node) => {
            const option = document.createElement("option");
            option.value = node.node;
            const blockReason = !node.allowed
              ? node.reason || "blocked"
              : node.cpu_ok === false
                ? node.cpu_reason || "CPU incompatible"
                : "";
            option.textContent = blockReason ? `${node.node} — ${blockReason}` : node.node;
            option.dataset.allowed = node.allowed ? "true" : "false";
            option.dataset.reason = node.reason || "";
            option.dataset.cpuOk = node.cpu_ok === false ? "false" : "true";
            option.dataset.cpuReason = node.cpu_reason || "";
            option.dataset.hostCpuMatch = node.host_cpu_match === false ? "false" : "true";
            option.dataset.hostCpuReason = node.host_cpu_reason || "";
            nodeSelect.appendChild(option);
          });
          const firstAllowed =
            nodes.find((node) => node.allowed && node.cpu_ok !== false) || nodes.find((node) => node.allowed);
          nodeSelect.value = firstAllowed ? firstAllowed.node : nodes[0].node;
          nodeSelect.disabled = false;
        }
      }
      if (submitButton) {
        submitButton.disabled = false;
      }
      syncFields();
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load migrate options.";
        error.hidden = false;
      }
    });
};

const openCloneDialog = (overview, rows, { toTemplate = false } = {}) => {
  const row = rows[0];
  const label = row?.dataset.guestLabel || "guest";
  const guestName = row?.dataset.guestName || "";
  let usedVmids = new Set();
  const dialog = openVmFormDialog({
    title: toTemplate
      ? "Clone to Template"
      : row?.dataset.guestTemplate === "true"
        ? "New VM from This Template"
        : "Clone",
    summary: label,
    submitLabel: toTemplate ? "Clone to Template" : "Clone",
    bodyHtml: `
        <label class="form-field">
          <span>New VMID</span>
          <input type="number" name="clone_newid" min="1" step="1" required disabled>
        </label>
        <label class="form-field">
          <span>Name</span>
          <input type="text" name="clone_name" autocomplete="off" required value="${escapeHtml(guestName ? `${guestName}-clone` : "")}">
        </label>
        <label class="form-field">
          <span>Storage</span>
          <select name="clone_storage" disabled>
            <option value="">Loading storages...</option>
          </select>
        </label>
        ${
          toTemplate
            ? '<p class="form-hint">Creates a full clone and converts the new VM to a template after the clone completes.</p>'
            : `<label class="form-field form-field-inline">
          <input type="checkbox" name="clone_full" value="1" checked>
          <span>Full clone</span>
        </label>`
        }
        <p class="form-hint" data-clone-full-hint hidden></p>
      `,
    onSubmit: (formData) => {
      const newid = String(formData.get("clone_newid") || "").trim();
      if (!/^[0-9]+$/.test(newid) || Number(newid) <= 0) {
        return "New VMID must be a positive whole number.";
      }
      if (usedVmids.has(Number(newid))) {
        return `VMID ${newid} is already in use — pick a free ID.`;
      }
      const name = String(formData.get("clone_name") || "").trim();
      if (!name) {
        return "Name is required.";
      }
      const fields = {
        clone_newid: newid,
        clone_name: name,
        clone_storage: String(formData.get("clone_storage") || "").trim(),
        clone_full: fullCheckbox?.checked ? "1" : "0",
      };
      if (toTemplate) {
        const { type, vmid } = guestRowIdentity(row);
        const url = `/vms/${encodeURIComponent(type)}/${encodeURIComponent(vmid)}/clone-to-template/`;
        submitVmBulkAction(overview, "clone_to_template", fields, rows, url);
      } else {
        submitVmBulkAction(overview, "clone", fields, rows);
      }
      return "";
    },
  });
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const idInput = dialog?.querySelector("[name='clone_newid']");
  const storageSelect = dialog?.querySelector("[name='clone_storage']");
  const fullCheckbox = dialog?.querySelector("[name='clone_full']");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  const syncStorageState = () => {
    if (storageSelect) {
      storageSelect.disabled =
        toTemplate || Boolean(fullCheckbox && !fullCheckbox.checked) || storageSelect.options.length === 0;
    }
  };
  fullCheckbox?.addEventListener("change", syncStorageState);
  const optionsUrl = row?.dataset.cloneOptionsUrl || "";
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve clone options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load clone options.");
      }
      return response.json();
    })
    .then((data) => {
      usedVmids = new Set((Array.isArray(data.used_vmids) ? data.used_vmids : []).map(Number));
      if (idInput) {
        idInput.value = data.nextid || "";
        idInput.disabled = false;
        // Live feedback so a taken ID is caught before submit, not after.
        const validateId = () => {
          const value = Number(idInput.value);
          const taken = Number.isFinite(value) && value > 0 && usedVmids.has(value);
          idInput.setCustomValidity(taken ? "VMID already in use" : "");
          if (error) {
            if (taken) {
              error.textContent = `VMID ${idInput.value} is already in use — pick a free ID.`;
              error.hidden = false;
            } else if (error.textContent.includes("already in use")) {
              error.hidden = true;
            }
          }
        };
        idInput.addEventListener("input", validateId);
      }
      if (storageSelect) {
        storageSelect.innerHTML = "";
        const storages = Array.isArray(data.storages) ? data.storages : [];
        if (storages.length === 0) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "Same/default storage";
          storageSelect.appendChild(option);
        } else {
          storages.forEach((storage) => {
            const option = document.createElement("option");
            option.value = storage.id || "";
            option.textContent = storage.label || storage.id || "";
            if (storage.id === data.default_storage) {
              option.selected = true;
            }
            storageSelect.appendChild(option);
          });
        }
      }
      const nameInput = dialog?.querySelector("[name='clone_name']");
      if (nameInput && !nameInput.value && data.suggested_name) {
        nameInput.value = data.suggested_name;
      }
      // Linked clones are only valid from a template; force a full clone for a
      // regular guest so Proxmox does not reject it.
      const fullHint = dialog?.querySelector("[data-clone-full-hint]");
      if (fullCheckbox && !data.is_template) {
        fullCheckbox.checked = true;
        fullCheckbox.disabled = true;
        if (fullHint) {
          fullHint.textContent = "Linked clones require a template — this guest will be full-cloned.";
          fullHint.hidden = false;
        }
      }
      syncStorageState();
      if (submitButton) {
        submitButton.disabled = false;
      }
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load clone options.";
        error.hidden = false;
      }
    });
};

const openDestroyDialog = (overview, rows) => {
  const row = rows[0];
  const label = row?.dataset.guestLabel || "guest";
  const target = row?.dataset.guestTarget || "";
  // target is like "vm:986@pve3" — confirm on the numeric VMID only.
  const vmid = (target.split(":")[1] || "").split("@")[0];
  openVmFormDialog({
    title: "Remove VM/CT",
    summary: label,
    submitLabel: "Remove",
    submitClass: "primary-action danger-action",
    bodyHtml: `
        <div class="danger-confirmation">
          <i data-lucide="triangle-alert" aria-hidden="true"></i>
          <div>
            <p>Destroy this guest and remove it from Proxmox inventory.</p>
            <p class="warning-copy">Referenced disks will be destroyed by Proxmox.</p>
          </div>
        </div>
        <label class="form-field">
          <span>Enter VMID to confirm (${escapeHtml(vmid)})</span>
          <input type="number" name="destroy_confirm_vmid" min="1" step="1" autocomplete="off" required>
        </label>
        <label class="form-field form-field-inline">
          <input type="checkbox" name="destroy_purge" value="1" checked>
          <span>Purge from job configurations</span>
        </label>
        <label class="form-field form-field-inline">
          <input type="checkbox" name="destroy_unreferenced_disks" value="1" checked>
          <span>Destroy unreferenced disks owned by guest</span>
        </label>
      `,
    onSubmit: (formData) => {
      const confirmed = String(formData.get("destroy_confirm_vmid") || "").trim();
      if (confirmed !== vmid) {
        return `Enter ${vmid} to confirm.`;
      }
      submitVmBulkAction(
        overview,
        "destroy",
        {
          destroy_confirm_vmid: confirmed,
          destroy_purge: formData.get("destroy_purge") === "1" ? "1" : "0",
          destroy_unreferenced_disks: formData.get("destroy_unreferenced_disks") === "1" ? "1" : "0",
        },
        rows
      );
      return "";
    },
  });
  createIcons();
};

const openUnTemplateDialog = (overview, rows) => {
  const row = rows[0];
  const label = row?.dataset.guestLabel || row?.dataset.guestName || "Template";
  const target = row?.dataset.guestTarget || "";
  const vmid = (target.split(":")[1] || "").split("@")[0];
  openVmFormDialog({
    title: "Convert Template Back to VM",
    summary: label,
    submitLabel: "Convert to VM",
    submitClass: "primary-action danger-action",
    bodyHtml: `
        <div class="danger-confirmation">
          <i data-lucide="triangle-alert" aria-hidden="true"></i>
          <div>
            <p>This clears the Proxmox template flag and makes the original guest a VM again.</p>
            <p class="warning-copy">Proxmox does not provide an officially supported reverse for this operation. Linked clones, snapshots, locked templates, protected templates, and unsupported storage are blocked.</p>
          </div>
        </div>
        <label class="form-field form-field-inline">
          <input type="checkbox" name="untemplate_acknowledge" value="convert" required>
          <span>I understand that this changes the template back into its original VM.</span>
        </label>
        <label class="form-field">
          <span>Enter VMID to confirm (${escapeHtml(vmid)})</span>
          <input type="number" name="untemplate_confirm_vmid" min="1" step="1" autocomplete="off" required>
        </label>
      `,
    onSubmit: (formData) => {
      const confirmed = String(formData.get("untemplate_confirm_vmid") || "").trim();
      if (confirmed !== vmid) {
        return `Enter ${vmid} to confirm.`;
      }
      if (formData.get("untemplate_acknowledge") !== "convert") {
        return "Confirm that you understand this operation.";
      }
      submitVmBulkAction(
        overview,
        "untemplate",
        {
          untemplate_confirm_vmid: confirmed,
          untemplate_acknowledge: "convert",
        },
        rows
      );
      return "";
    },
  });
  createIcons();
};

const positionContextMenu = (menu, event) => {
  const margin = 8;
  menu.hidden = false;
  menu.style.visibility = "hidden";
  menu.style.left = "0px";
  menu.style.top = "0px";
  const rect = menu.getBoundingClientRect();
  const left = Math.max(margin, Math.min(event.clientX, window.innerWidth - rect.width - margin));
  const top = Math.max(margin, Math.min(event.clientY, window.innerHeight - rect.height - margin));
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;
  menu.style.visibility = "";
};

const openVmContextMenu = (menu, row, event) => {
  const overview = row.closest("[data-vm-overview]");
  if (!overview) {
    return false;
  }

  clearVmContextHighlights();
  const selectedRows = selectedVmOverviewRows(overview);
  const rowCheckbox = row.querySelector("[data-vm-select]");
  const contextRows = rowCheckbox?.checked && selectedRows.length ? selectedRows : [row];
  contextRows.forEach((item) => {
    item.classList.add("context-selected");
  });

  const selectedCount = contextRows.length;
  const allRunning = contextRows.every((item) => item.dataset.guestStatus === "running");
  const allNotRunning = contextRows.every((item) => item.dataset.guestStatus !== "running");
  const allStopped = contextRows.every((item) => item.dataset.guestStatus === "stopped");
  const allPaused = contextRows.every((item) => item.dataset.guestStatus === "paused");
  const allVms = contextRows.every((item) => item.dataset.guestType === "vm");
  const noTemplates = contextRows.every((item) => item.dataset.guestTemplate !== "true");
  const allTemplates = contextRows.every((item) => item.dataset.guestTemplate === "true");
  // A linked clone must not become a template — it would seed a fragile,
  // chained lineage. Full-clone it first.
  const noLinkedClones = contextRows.every((item) => item.dataset.guestLinkedClone !== "true");
  const allAgentEnabled = contextRows.every((item) => item.dataset.guestAgentEnabled === "true");
  const allAgentDisabled = contextRows.every((item) => item.dataset.guestAgentEnabled !== "true");
  const hasAssignedTags = contextRows.some((item) => guestRowTags(item).length > 0);
  const singleSelected = contextRows.length === 1;
  const writable = true;

  activeVmOverview = overview;
  activeVmContextRows = contextRows;
  activeLabel = "";
  menu.innerHTML = `
      <div class="context-menu-title">Actions - ${selectedCount} Object${selectedCount === 1 ? "" : "s"}</div>
      <button type="button" data-vm-action="open-summary" ${singleSelected ? "" : "disabled"}>Open Summary</button>
      <button type="button" data-vm-action="edit-hardware" ${singleSelected && writable ? "" : "disabled"}>Edit Hardware...</button>
      <div class="context-menu-separator"></div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Power <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="start" ${writable && allNotRunning && !allPaused ? "" : "disabled"}><i data-lucide="play" aria-hidden="true"></i>Power On</button>
          <button type="button" data-vm-action="resume" ${writable && allPaused && allVms ? "" : "disabled"}><i data-lucide="play" aria-hidden="true"></i>Resume</button>
          <button type="button" data-vm-action="stop" ${writable && allRunning ? "" : "disabled"}><i data-lucide="square" aria-hidden="true"></i>Power Off</button>
          <button type="button" data-vm-action="reset" ${writable && allRunning && allVms ? "" : "disabled"}><i data-lucide="rotate-ccw" aria-hidden="true"></i>Reset</button>
          <div class="context-menu-separator"></div>
          <button type="button" data-vm-action="suspend" ${writable && allRunning && allVms ? "" : "disabled"}><i data-lucide="pause" aria-hidden="true"></i>Suspend (to RAM)</button>
          <button type="button" data-vm-action="hibernate" ${writable && allRunning && allVms ? "" : "disabled"}><i data-lucide="moon" aria-hidden="true"></i>Hibernate (to disk)</button>
          <div class="context-menu-separator"></div>
          <button type="button" data-vm-action="shutdown" ${writable && allRunning ? "" : "disabled"}><i data-lucide="power" aria-hidden="true"></i>Shut Down Guest OS</button>
          <button type="button" data-vm-action="reboot" ${writable && allRunning ? "" : "disabled"}><i data-lucide="refresh-cw" aria-hidden="true"></i>Restart Guest OS</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Guest OS <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="open-summary" ${singleSelected ? "" : "disabled"}>Open Summary</button>
          <button type="button" data-vm-action="agent_enable" ${writable && allVms && allAgentDisabled ? "" : "disabled"}>Enable guest agent</button>
          <button type="button" data-vm-action="agent_disable" ${writable && allVms && allAgentEnabled ? "" : "disabled"}>Disable guest agent</button>
          <button type="button" disabled>Run command</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Snapshots <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="snapshot" ${writable ? "" : "disabled"}><i data-lucide="camera" aria-hidden="true"></i>Take Snapshot...</button>
          <button type="button" data-vm-action="open-snapshots" ${singleSelected ? "" : "disabled"}>Manage Snapshots</button>
          <button type="button" data-vm-action="delete-snapshots" ${writable ? "" : "disabled"}>Delete All Snapshots...</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Backup <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="backup" ${writable ? "" : "disabled"}><i data-lucide="archive" aria-hidden="true"></i>Back Up Now...</button>
          <button type="button" data-vm-action="open-backup" ${singleSelected ? "" : "disabled"}>Manage Backups</button>
          <button type="button" data-vm-action="restore-backup" ${singleSelected && writable ? "" : "disabled"}>Restore Backup...</button>
        </div>
      </div>
      <div class="context-menu-separator"></div>
      <button type="button" data-vm-action="migrate" ${writable ? "" : "disabled"}><i data-lucide="move-right" aria-hidden="true"></i>Migrate...</button>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Template <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="clone" ${singleSelected && writable ? "" : "disabled"}>${allTemplates ? "New VM from This Template..." : "Clone..."}</button>
          ${allTemplates ? `<button type="button" data-vm-action="clone-to-template" ${singleSelected && writable ? "" : "disabled"}>Clone to Template...</button>` : ""}
          <button type="button" data-vm-action="template" ${writable && allStopped && allVms && noTemplates && noLinkedClones ? "" : "disabled"}>Convert to Template</button>
          <button type="button" data-vm-action="untemplate" ${singleSelected && writable && allStopped && allVms && allTemplates ? "" : "disabled"}>Convert Template to VM...</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Tags <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="add-tags" ${writable ? "" : "disabled"}>Add Tags...</button>
          <button type="button" data-vm-action="remove-tags" ${writable && hasAssignedTags ? "" : "disabled"}>Remove Tags...</button>
        </div>
      </div>
      <button type="button" data-vm-action="pool" ${writable ? "" : "disabled"}>Move to Pool...</button>
      <div class="context-menu-separator"></div>
      <button type="button" data-vm-action="destroy" class="danger" ${singleSelected && writable && allStopped ? "" : "disabled"}>Remove from Disk...</button>
    `;
  createIcons();
  positionContextMenu(menu, event);
  return true;
};

const setTaskRowCancelled = (row) => {
  if (!row) {
    return;
  }
  const now = new Date();
  const statusCell = row.querySelector('[data-column="status"]');
  const detailsCell = row.querySelector('[data-column="details"]');
  const finishedCell = row.querySelector('[data-column="finished"]');
  if (statusCell) {
    statusCell.dataset.sortValue = "Cancelled";
    statusCell.innerHTML = '<span class="badge cancelled">Cancelled</span>';
  }
  if (detailsCell) {
    detailsCell.dataset.sortValue = "Cancelled by user";
    detailsCell.textContent = "Cancelled by user";
  }
  if (finishedCell) {
    finishedCell.dataset.sortValue = String(now.getTime());
    finishedCell.textContent = taskDateLabel(now);
  }
  row.dataset.taskCancelable = "false";
  row.dataset.taskRowSignature = "";
};

const openTaskContextMenu = (menu, row, event) => {
  const taskbar = row.closest("[data-recent-tasks]");
  if (!taskbar) {
    return false;
  }
  const taskName = row.querySelector('[data-column="task-name"]')?.textContent?.trim() || "Task";
  const cancelable = row.dataset.taskCancelable === "true";
  activeTaskRow = row;
  activeVmOverview = null;
  activeVmContextRows = [];
  activeLabel = "";
  clearVmContextHighlights();
  menu.innerHTML = `
      <div class="context-menu-title">${escapeHtml(taskName)}</div>
      <button type="button" data-task-action="cancel-task" ${cancelable ? "" : "disabled"}>Cancel Task</button>
    `;
  positionContextMenu(menu, event);
  return true;
};

const initContextMenu = () => {
  const menu = document.getElementById("context-menu");
  if (!menu || menu.dataset.initialized === "true") {
    return;
  }

  menu.dataset.initialized = "true";
  document.addEventListener("contextmenu", (event) => {
    const vmRow = event.target.closest("[data-vm-overview-row]");
    if (vmRow && openVmContextMenu(menu, vmRow, event)) {
      event.preventDefault();
      return;
    }

    const taskRow = event.target.closest("[data-task-row-key]");
    if (taskRow && openTaskContextMenu(menu, taskRow, event)) {
      event.preventDefault();
      return;
    }

    const row = event.target.closest("[data-context-label]");
    if (!row) {
      return;
    }

    event.preventDefault();
    activeVmOverview = null;
    clearVmContextHighlights();
    activeLabel = row.dataset.contextLabel || "";
    menu.innerHTML = `
        <button type="button" data-action="details">Details</button>
        <button type="button" data-action="copy-path">Copy path</button>
      `;
    positionContextMenu(menu, event);
  });

  document.addEventListener("click", (event) => {
    if (!menu.contains(event.target)) {
      menu.hidden = true;
      activeTaskRow = null;
      clearVmContextHighlights();
    }
  });

  menu.addEventListener("click", async (event) => {
    const taskButton = event.target.closest("button[data-task-action]");
    if (taskButton && activeTaskRow) {
      event.preventDefault();
      if (taskButton.disabled) {
        return;
      }
      const action = taskButton.dataset.taskAction || "";
      const taskbar = activeTaskRow.closest("[data-recent-tasks]");
      const cancelUrl = taskbar?.dataset.taskCancelUrl || "";
      const taskId = activeTaskRow.dataset.taskId || activeTaskRow.dataset.taskRowKey || "";
      if (action === "cancel-task" && cancelUrl && taskId) {
        if (
          !(await openConfirmDialog({
            title: "Cancel task",
            body: "<p>Cancel this task?</p>",
            confirmLabel: "Cancel task",
          }))
        ) {
          menu.hidden = true;
          activeTaskRow = null;
          return;
        }
        const body = new URLSearchParams();
        body.set("task_id", taskId);
        try {
          const response = await fetch(new URL(cancelUrl, window.location.origin), {
            method: "POST",
            headers: {
              "Content-Type": "application/x-www-form-urlencoded",
              "X-CSRFToken": taskbar?.dataset.csrfToken || "",
              "X-Requested-With": "fetch",
            },
            body,
          });
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(payload.error || "Task could not be cancelled.");
          }
          setTaskRowCancelled(activeTaskRow);
          window.pveHelperRefreshRecentTasks?.();
        } catch (error) {
          window.alert(error.message || "Task could not be cancelled.");
        }
      }
      menu.hidden = true;
      activeTaskRow = null;
      return;
    }

    const vmButton = event.target.closest("button[data-vm-action]");
    if (vmButton && activeVmOverview) {
      const targetRows = activeVmContextRows.length ? activeVmContextRows : selectedVmOverviewRows(activeVmOverview);
      const firstRow = targetRows[0];
      const action = vmButton.dataset.vmAction || "";
      if (vmButton.disabled || !firstRow) {
        return;
      }
      if (action === "open-summary") {
        loadSoftNavigation(new URL(firstRow.dataset.detailUrl || window.location.href, window.location.origin));
        return;
      }
      if (action === "edit-hardware") {
        loadSoftNavigation(
          new URL(
            firstRow.dataset.editHardwareUrl ||
              firstRow.dataset.editOptionsUrl ||
              firstRow.dataset.detailUrl ||
              window.location.href,
            window.location.origin
          )
        );
        return;
      }
      if (action === "open-snapshots") {
        loadSoftNavigation(new URL(firstRow.dataset.snapshotsUrl || window.location.href, window.location.origin));
        return;
      }
      if (action === "open-backup") {
        loadSoftNavigation(new URL(firstRow.dataset.backupUrl || window.location.href, window.location.origin));
        return;
      }
      if (action === "restore-backup") {
        const restoreUrl = new URL("/vms/restore/", window.location.origin);
        const targetParts = String(firstRow.dataset.guestTarget || "")
          .split("@")[0]
          .split(":");
        restoreUrl.searchParams.set("source_type", firstRow.dataset.guestType || targetParts[0] || "");
        restoreUrl.searchParams.set("source_vmid", firstRow.dataset.guestVmid || targetParts[1] || "");
        loadSoftNavigation(restoreUrl);
        return;
      }
      if (action === "add-tags" || action === "remove-tags") {
        openTagsDialog(activeVmOverview, targetRows, action === "add-tags" ? "add" : "remove");
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "pool") {
        openPoolDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "migrate") {
        openMigrateDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "snapshot") {
        openSnapshotDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "backup") {
        openBackupDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "clone") {
        openCloneDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "clone-to-template") {
        openCloneDialog(activeVmOverview, targetRows, { toTemplate: true });
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "destroy") {
        openDestroyDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (action === "untemplate") {
        openUnTemplateDialog(activeVmOverview, targetRows);
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (
        action === "delete-snapshots" &&
        !(await openConfirmDialog({
          title: "Delete all snapshots",
          body: `<p>Delete all snapshots for <strong>${targetRows.length}</strong> selected guest${targetRows.length === 1 ? "" : "s"}?</p><p>This cannot be undone.</p>`,
          confirmLabel: "Delete all",
          danger: true,
        }))
      ) {
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (
        ["stop", "reset"].includes(action) &&
        !(await openConfirmDialog({
          title: action === "reset" ? "Reset guests" : "Power off guests",
          body: `<p>${action === "reset" ? "Reset" : "Power off"} <strong>${targetRows.length}</strong> selected guest${targetRows.length === 1 ? "" : "s"}?</p>`,
          confirmLabel: action === "reset" ? "Reset" : "Power off",
          danger: true,
        }))
      ) {
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (
        action === "template" &&
        !(await openConfirmDialog({
          title: "Convert to template",
          body: `<p>Convert <strong>${targetRows.length}</strong> selected VM${targetRows.length === 1 ? "" : "s"} to template?</p>`,
          confirmLabel: "Convert",
        }))
      ) {
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      if (
        action === "hibernate" &&
        !(await openConfirmDialog({
          title: "Hibernate guests",
          body: `<p>Hibernate <strong>${targetRows.length}</strong> selected VM${targetRows.length === 1 ? "" : "s"}?</p><p>State is saved to disk and the VM stops; Power On resumes it.</p>`,
          confirmLabel: "Hibernate",
        }))
      ) {
        menu.hidden = true;
        clearVmContextHighlights();
        return;
      }
      submitVmBulkAction(activeVmOverview, action === "delete-snapshots" ? "delete_snapshots" : action, {}, targetRows);
      menu.hidden = true;
      clearVmContextHighlights();
      return;
    }

    const button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }

    if (button.dataset.action === "copy-path" && activeLabel) {
      await navigator.clipboard.writeText(activeLabel);
    }

    menu.hidden = true;
    clearVmContextHighlights();
  });
};

const shouldUseSoftNavigation = (anchor, event) => {
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey ||
    (anchor.target && anchor.target !== "_self") ||
    anchor.hasAttribute("download") ||
    anchor.closest("[data-no-soft-navigation]")
  ) {
    return false;
  }

  const url = new URL(anchor.href, window.location.href);
  if (url.origin !== window.location.origin) {
    return false;
  }
  if (url.pathname.startsWith("/auth/")) {
    return false;
  }
  if (url.pathname.includes("/download/")) {
    return false;
  }
  if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) {
    return false;
  }
  return true;
};

const setSoftNavigationLoading = (loading) => {
  document.documentElement.classList.toggle("soft-navigation-loading", loading);
  const content = document.querySelector(softContentSelector);
  if (content) {
    content.setAttribute("aria-busy", loading ? "true" : "false");
  }
};

const replacePageFromDocument = (nextDocument) => {
  const currentContent = document.querySelector(softContentSelector);
  const nextContent = nextDocument.querySelector(softContentSelector);
  const currentTree = document.querySelector(softTreeSelector);
  const nextTree = nextDocument.querySelector(softTreeSelector);
  const currentStatus = document.querySelector(softStatusSelector);
  const nextStatus = nextDocument.querySelector(softStatusSelector);

  if (!currentContent || !nextContent || !currentTree || !nextTree) {
    return false;
  }

  runPageCleanup();
  currentContent.innerHTML = nextContent.innerHTML;
  currentContent.scrollTop = 0;
  currentContent.focus({ preventScroll: true });
  currentTree.innerHTML = nextTree.innerHTML;
  if (currentStatus && nextStatus) {
    currentStatus.innerHTML = nextStatus.innerHTML;
  }
  document.title = nextDocument.title || "pve-helper";

  initTreeModules(document);
  refreshSidebarWidth();
  pageInitializer(currentContent);
  createIcons();
  return true;
};

const loadSoftNavigation = async (url, options = {}) => {
  const push = options.push !== false;

  const contextMenu = document.getElementById("context-menu");
  if (contextMenu) {
    contextMenu.hidden = true;
  }
  clearVmContextHighlights();

  if (navigationController) {
    navigationController.abort();
  }

  const controller = new AbortController();
  navigationController = controller;
  setSoftNavigationLoading(true);

  try {
    const response = await fetch(url.href, {
      headers: {
        Accept: "text/html",
        "X-Requested-With": "fetch",
      },
      signal: controller.signal,
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || !contentType.includes("text/html")) {
      throw new Error("Soft navigation response was not HTML.");
    }
    if (response.redirected && new URL(response.url).origin !== window.location.origin) {
      window.location.assign(response.url);
      return;
    }

    const html = await response.text();
    const nextDocument = new DOMParser().parseFromString(html, "text/html");
    if (!replacePageFromDocument(nextDocument)) {
      throw new Error("Soft navigation shell markers were missing.");
    }
    if (push) {
      window.history.pushState({ softNavigation: true }, "", url.href);
    }
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    window.location.assign(url.href);
  } finally {
    if (navigationController === controller) {
      navigationController = null;
      setSoftNavigationLoading(false);
    }
  }
};

const initSoftNavigation = () => {
  if (document.documentElement.dataset.softNavigationInitialized === "true") {
    return;
  }

  document.documentElement.dataset.softNavigationInitialized = "true";
  if ("scrollRestoration" in window.history) {
    window.history.scrollRestoration = "manual";
  }

  document.addEventListener("click", (event) => {
    const anchor = event.target.closest("a[href]");
    if (!anchor || !shouldUseSoftNavigation(anchor, event)) {
      return;
    }

    event.preventDefault();
    const url = new URL(anchor.href, window.location.href);
    loadSoftNavigation(url);
  });

  window.addEventListener("popstate", () => {
    loadSoftNavigation(new URL(window.location.href), { push: false });
  });
};

export {
  clearVmContextHighlights,
  createPendingGuestFormTask,
  createPendingVmTask,
  defaultSnapshotName,
  dismissTaskQuestion,
  ensureVmActionDialog,
  forceStopGuest,
  formatMigrateBytes,
  guestRowIdentity,
  initBackupRestoreForms,
  initContextMenu,
  initGuestActionForms,
  initSoftNavigation,
  loadSoftNavigation,
  openBackupDialog,
  openBulkMigrateDialog,
  openCloneDialog,
  openConfirmDialog,
  openDestroyDialog,
  openForceStopDialog,
  openInputDialog,
  openMigrateDialog,
  openPoolDialog,
  openSnapshotDialog,
  openTagsDialog,
  openTaskContextMenu,
  openUnTemplateDialog,
  openVmContextMenu,
  openVmFormDialog,
  positionContextMenu,
  replacePageFromDocument,
  selectedGuestSummary,
  setPageInitializer,
  setSoftNavigationLoading,
  setTaskRowCancelled,
  shouldUseSoftNavigation,
  submitVmBulkAction,
  updateVmRowsAgentState,
  updateVmRowsPoolState,
  updateVmRowsTemplateState,
};
