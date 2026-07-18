import { openConfirmDialog } from "./dialogs.js";
import { clearLocalError, showLocalError } from "./feedback.js";
import { openBackupDialog, openSnapshotDialog, submitVmBulkAction } from "./guest-actions.js";
import {
  openCloneDialog,
  openDestroyDialog,
  openMigrateDialog,
  openUnTemplateDialog,
} from "./guest-mobility-actions.js";
import { openPoolDialog, openTagsDialog } from "./guest-tag-pool-actions.js";
import { loadSoftNavigation } from "./navigation.js";
import { selectedVmOverviewRows } from "./scheduling.js";
import { createIcons, escapeHtml, parseGuestRef, taskDateLabel } from "./shell.js";

let activeLabel = "";
let activeVmOverview = null;
let activeVmContextRows = [];
let activeTaskRow = null;

const clearVmContextHighlights = () => {
  document.querySelectorAll("[data-vm-overview-row].context-selected").forEach((row) => {
    row.classList.remove("context-selected");
  });
  activeVmContextRows = [];
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
          <button type="button" data-vm-action="remove-tags" ${writable ? "" : "disabled"}>Remove Tags...</button>
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
  const retryable = row.dataset.taskRetryable === "true";
  activeTaskRow = row;
  activeVmOverview = null;
  activeVmContextRows = [];
  activeLabel = "";
  clearVmContextHighlights();
  menu.innerHTML = `
      <div class="context-menu-title">${escapeHtml(taskName)}</div>
      <button type="button" data-task-action="cancel-task" ${cancelable ? "" : "disabled"}>Cancel Task</button>
      <button type="button" data-task-action="retry-task" ${retryable ? "" : "disabled"}>Retry Task...</button>
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
      const retryUrl = taskbar?.dataset.taskRetryUrl || "";
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
        clearLocalError(taskbar);
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
          showLocalError(taskbar, error.message || "Task could not be cancelled.");
        }
      } else if (action === "retry-task" && retryUrl && taskId) {
        if (
          !(await openConfirmDialog({
            title: "Retry tag operation",
            body: "<p>Retry the failed parts of this tag operation?</p>",
            confirmLabel: "Retry",
          }))
        ) {
          menu.hidden = true;
          activeTaskRow = null;
          return;
        }
        const body = new URLSearchParams();
        body.set("task_id", taskId);
        clearLocalError(taskbar);
        try {
          const response = await fetch(new URL(retryUrl, window.location.origin), {
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
            throw new Error(payload.error || "Tag operation could not be retried.");
          }
          activeTaskRow.dataset.taskRetryable = "false";
          window.pveHelperRefreshRecentTasks?.();
        } catch (error) {
          showLocalError(taskbar, error.message || "Tag operation could not be retried.");
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
        const target = parseGuestRef(firstRow.dataset.guestRef || firstRow.dataset.guestTarget || "");
        if (!target.cluster) return;
        const restoreUrl = new URL(`/vms/${encodeURIComponent(target.cluster)}/restore/`, window.location.origin);
        restoreUrl.searchParams.set("source_type", firstRow.dataset.guestType || target.type);
        restoreUrl.searchParams.set("source_vmid", firstRow.dataset.guestVmid || target.vmid);
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

export {
  clearVmContextHighlights,
  initContextMenu,
  openTaskContextMenu,
  openVmContextMenu,
  positionContextMenu,
  setTaskRowCancelled,
};
