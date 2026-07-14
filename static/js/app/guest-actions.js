import { ensureVmActionDialog, openConfirmDialog, openInputDialog } from "./dialogs.js";
import { clearLocalError, showLocalError } from "./feedback.js";
import { loadSoftNavigation } from "./navigation.js";
import { selectedVmOverviewRows } from "./scheduling.js";
import { addPendingRecentTask, escapeHtml, renderVIcons, taskDateLabel, updatePendingRecentTask } from "./shell.js";
import { pendingVmTaskDetails, pendingVmTaskTarget, vmActionAuditAction, vmActionTaskName } from "./vm-overview.js";

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
      clearLocalError(form);
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
        showLocalError(form, message);
      };
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { Accept: "application/json", "X-Requested-With": "fetch" },
        });
        const payload = await response.json().catch(() => ({}));
        if (!payload.ok) {
          fail((payload.errors || [`Action failed: HTTP ${response.status}.`]).join("; "));
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
  clearLocalError(overview);
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
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      requestSettled = true;
      const message = (payload.errors || [`VM/CT action failed: HTTP ${response.status}.`]).join("; ");
      updatePendingRecentTask({
        id: pendingTask.id,
        status: "Failed",
        status_class: "failed",
        details: message,
        finished_at: taskDateLabel(new Date()),
        finished_at_ms: Date.now(),
      });
      showLocalError(overview, message);
      return;
    }
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
      showLocalError(overview, (payload.errors || ["VM/CT action failed."]).join("; "));
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
        action === "destroy" ||
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
        if (action === "destroy") {
          const destination = window.location.pathname.startsWith("/vms/overview/") ? "/vms/overview/" : "/vms/";
          await loadSoftNavigation(new URL(destination, window.location.origin));
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
    showLocalError(overview, "VM/CT action failed: network error.");
  }
};

const defaultSnapshotName = () => {
  const now = new Date();
  const pad = (value) => String(value).padStart(2, "0");
  return `manual_${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
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

export {
  createPendingGuestFormTask,
  createPendingVmTask,
  defaultSnapshotName,
  dismissTaskQuestion,
  ensureVmActionDialog,
  forceStopGuest,
  guestRowIdentity,
  initBackupRestoreForms,
  initGuestActionForms,
  openBackupDialog,
  openConfirmDialog,
  openForceStopDialog,
  openInputDialog,
  openSnapshotDialog,
  openVmFormDialog,
  selectedGuestSummary,
  submitVmBulkAction,
  updateVmRowsAgentState,
  updateVmRowsPoolState,
  updateVmRowsTemplateState,
};
