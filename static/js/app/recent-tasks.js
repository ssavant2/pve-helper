import { initGuestActionForms, openForceStopDialog } from "./guest-actions.js";
import { loadSoftNavigation } from "./navigation.js";
import {
  activeUploads,
  applyGuestStatusHintsFromTasks,
  createIcons,
  escapeHtml,
  parseGuestRef,
  recentTasksRefreshEvent,
  refreshGuestStateAfterTaskTransitions,
  renderGuestLabel,
} from "./shell.js";
import { openBulkFilePartialDialog } from "./storage-browser.js";

const initRecentTasks = () => {
  const recentTasks = document.querySelector("[data-recent-tasks]");
  if (!recentTasks || recentTasks.dataset.initialized === "true") {
    return;
  }

  recentTasks.dataset.initialized = "true";
  recentTasks.addEventListener("click", (event) => {
    const question = event.target.closest("[data-task-question]");
    if (!question || !recentTasks.contains(question)) {
      return;
    }
    event.preventDefault();
    const taskId = question.dataset.taskQuestionId || "";
    let payload = {};
    try {
      payload = JSON.parse(question.dataset.taskQuestionPayload || "{}");
    } catch (_error) {
      payload = {};
    }
    if (question.dataset.taskQuestion === "force_stop") {
      openForceStopDialog(payload.target || "", payload.label || "", taskId);
      return;
    }
    if (question.dataset.taskQuestion === "bulk_file_partial") {
      openBulkFilePartialDialog(payload, taskId);
    }
  });
  const rows = recentTasks.querySelector("[data-task-rows]");
  const previousButton = recentTasks.querySelector("[data-task-prev]");
  const nextButton = recentTasks.querySelector("[data-task-next]");
  const pageLabel = recentTasks.querySelector("[data-task-page-label]");
  const clusterFilter = recentTasks.querySelector("[data-task-cluster-filter]");
  const tasksUrl = recentTasks.dataset.tasksUrl;
  const pollMs = Number.parseInt(recentTasks.dataset.taskPollMs || "10000", 10);
  const parsedRenderedAtMs = Date.parse(recentTasks.dataset.taskRenderedAt || "");
  const renderedAtMs = Number.isFinite(parsedRenderedAtMs) ? parsedRenderedAtMs : Date.now();
  let taskPage = Number.parseInt(recentTasks.dataset.taskPage || "0", 10);
  let loadingTasks = false;
  let pendingLoadPage = null;
  let storageReloadPending = false;
  let pendingTasks = [];
  let lastLoadedTasks = [];
  let renderedTaskSignature = "";
  let taskStatusesById = new Map();
  let lastTaskPageData = {
    page: taskPage,
    total: 0,
    start_index: 0,
    end_index: 0,
    has_previous: false,
    has_next: false,
  };
  const pendingTasksForSelectedCluster = () => {
    const selectedCluster = clusterFilter?.value || "";
    return pendingTasks.filter((task) => !selectedCluster || !task.cluster_key || task.cluster_key === selectedCluster);
  };

  const taskSeenKey = (task) =>
    `pve-helper-reloaded-task-${task.id || `${task.action}:${task.storage_id}:${task.path}`}`;

  const taskWasReloaded = (task) => {
    try {
      return sessionStorage.getItem(taskSeenKey(task)) === "true";
    } catch (_error) {
      return false;
    }
  };

  const rememberTaskReload = (task) => {
    try {
      sessionStorage.setItem(taskSeenKey(task), "true");
    } catch (_error) {
      // Reloading is still safe without session storage; the timestamp gate prevents old events.
    }
  };

  const maybeRefreshCurrentStorageBrowser = (tasks) => {
    if (storageReloadPending) {
      return true;
    }

    const manager = document.querySelector("[data-storage-file-manager]");
    if (!manager) {
      return false;
    }

    const storageId = manager.dataset.storageId || "";
    const currentPath = manager.dataset.currentPath || "";
    const completedInflate = tasks.find((task) => {
      if (task.action !== "file.inflated" || task.storage_id !== storageId) {
        return false;
      }
      if ((task.path_parent || "") !== currentPath) {
        return false;
      }
      if (Number(task.finished_at_ms || 0) < renderedAtMs - 15000) {
        return false;
      }
      return !taskWasReloaded(task);
    });

    if (!completedInflate) {
      return false;
    }

    rememberTaskReload(completedInflate);
    storageReloadPending = true;
    Promise.resolve(manager.refreshCurrentFileRows?.()).finally(() => {
      storageReloadPending = false;
    });
    return false;
  };

  let snapshotRefreshPending = false;
  const refreshCurrentSnapshotView = async (snapshotView) => {
    if (!snapshotView || snapshotRefreshPending) {
      return;
    }
    const panel = snapshotView.querySelector("[data-snapshot-list-panel]");
    if (!panel) {
      return;
    }
    snapshotRefreshPending = true;
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("snapshot_partial", "1");
      const response = await fetch(url.href, {
        headers: {
          Accept: "application/json",
          "X-Requested-With": "fetch",
        },
      });
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      if (data.html) {
        panel.outerHTML = data.html;
        snapshotView.dataset.renderedAtMs = String(data.rendered_at_ms || Date.now());
        createIcons();
      }
    } catch (_error) {
      // Snapshot list refresh is best effort; manual navigation still shows the latest state.
    } finally {
      snapshotRefreshPending = false;
    }
  };

  const maybeRefreshSnapshotState = (tasks) => {
    const snapshotView = document.querySelector("[data-guest-snapshots]");
    const objectType = snapshotView?.dataset.objectType || "";
    const vmid = String(snapshotView?.dataset.vmid || "");
    const snapshotRenderedAtMs = Number(snapshotView?.dataset.renderedAtMs || 0);
    const completedSnapshotTask = tasks.find((task) => {
      if (!String(task.action || "").startsWith("guest.snapshot.")) {
        return false;
      }
      if (task.status_class !== "completed") {
        return false;
      }
      if (snapshotView) {
        const target = task.target_guest || {};
        if (String(target.type || "") !== objectType || String(target.vmid || "") !== vmid) {
          return false;
        }
        if (Number(task.finished_at_ms || 0) <= snapshotRenderedAtMs) {
          return false;
        }
      }
      return !taskWasReloaded(task);
    });

    if (!completedSnapshotTask) {
      return false;
    }

    rememberTaskReload(completedSnapshotTask);
    if (snapshotView) {
      refreshCurrentSnapshotView(snapshotView);
      return false;
    }
    document.querySelectorAll("[data-vm-overview]").forEach((overview) => {
      overview.refreshVmSnapshotInfo?.();
    });
    return false;
  };

  let backupRefreshPending = false;
  const refreshCurrentBackupView = async (backupView) => {
    if (!backupView || backupRefreshPending) {
      return;
    }
    const panel = backupView.querySelector("[data-backup-list-panel]");
    if (!panel) {
      return;
    }
    backupRefreshPending = true;
    try {
      const url = new URL(window.location.href);
      url.searchParams.set("backup_partial", "1");
      const response = await fetch(url.href, {
        headers: { Accept: "application/json", "X-Requested-With": "fetch" },
      });
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      if (data.html) {
        panel.outerHTML = data.html;
        backupView.dataset.renderedAtMs = String(data.rendered_at_ms || Date.now());
        createIcons();
        initGuestActionForms(backupView);
      }
    } catch (_error) {
      // The archive list is refreshed opportunistically after a tracked job.
    } finally {
      backupRefreshPending = false;
    }
  };

  const maybeRefreshBackupState = (tasks) => {
    const backupView = document.querySelector("[data-guest-backup]");
    if (!backupView) {
      return false;
    }
    const objectType = backupView.dataset.objectType || "";
    const vmid = String(backupView.dataset.vmid || "");
    const renderedAtMs = Number(backupView.dataset.renderedAtMs || 0);
    const completedTask = tasks.find((task) => {
      if (!["guest.backup.run", "guest.backup.restore", "guest.backup.delete"].includes(String(task.action || ""))) {
        return false;
      }
      if (task.status_class !== "completed" || Number(task.finished_at_ms || 0) <= renderedAtMs) {
        return false;
      }
      const target = task.target_guest || {};
      return String(target.type || "") === objectType && String(target.vmid || "") === vmid && !taskWasReloaded(task);
    });
    if (!completedTask) {
      return false;
    }
    rememberTaskReload(completedTask);
    refreshCurrentBackupView(backupView);
    return false;
  };

  const maybeRefreshCurrentGuestInventory = (tasks) => {
    const overview = document.querySelector("[data-vm-overview]");
    if (!overview) {
      return false;
    }

    const completedInventoryTask = tasks.find((task) => {
      if (
        !["guest.destroy", "guest.clone.create", "guest.template.clone", "guest.register.import"].includes(task.action)
      ) {
        return false;
      }
      if (task.status_class !== "completed") {
        return false;
      }
      if (taskWasReloaded(task)) {
        return false;
      }
      if (task.action === "guest.destroy") {
        const target = task.target_guest || {};
        const nodeSuffix = task.server && task.server !== "-" ? `@${task.server}` : "";
        const targetId = `${target.type || ""}:${target.vmid || ""}${nodeSuffix}`;
        const legacyTargetId = `${target.type || ""}:${target.vmid || ""}`;
        return Boolean(
          overview.querySelector(`[data-guest-target="${CSS.escape(targetId)}"]`) ||
            overview.querySelector(`[data-guest-target="${CSS.escape(legacyTargetId)}"]`)
        );
      }
      return Number(task.finished_at_ms || 0) >= renderedAtMs - 300000;
    });

    if (!completedInventoryTask) {
      return false;
    }

    rememberTaskReload(completedInventoryTask);
    loadSoftNavigation(new URL(window.location.href), { push: false });
    return true;
  };

  const maybeRefreshTagInventory = (tasks) => {
    const tagView = document.querySelector("[data-tag-inventory-view]");
    if (!tagView) {
      return false;
    }
    const renderedAtMs = Number(tagView.dataset.renderedAtMs || 0);
    const refreshTask = tasks.find((task) => {
      if (task.action !== "tag.inventory.refresh") {
        return false;
      }
      if (!["completed", "warning", "failed"].includes(task.status_class)) {
        return false;
      }
      return Number(task.finished_at_ms || 0) > renderedAtMs && !taskWasReloaded(task);
    });
    if (!refreshTask) {
      return false;
    }
    rememberTaskReload(refreshTask);
    loadSoftNavigation(new URL(window.location.href), { push: false });
    return true;
  };

  // A datastore page renders the published catalog. When a refresh of that
  // catalog finishes, everything on the page is one generation old — so re-render
  // it. This is the other half of the Refresh button: queueing is the request,
  // this is the answer arriving.
  const maybeRefreshStorageCatalogView = (tasks) => {
    const catalogView = document.querySelector("[data-storage-catalog-view]");
    if (!catalogView) {
      return false;
    }
    const catalogRenderedAtMs = Number(catalogView.dataset.renderedAtMs || 0);
    const refreshTask = tasks.find((task) => {
      if (task.action !== "storage.catalog.refresh") {
        return false;
      }
      if (!["completed", "warning", "failed"].includes(task.status_class)) {
        return false;
      }
      return Number(task.finished_at_ms || 0) > catalogRenderedAtMs && !taskWasReloaded(task);
    });
    if (!refreshTask) {
      return false;
    }
    rememberTaskReload(refreshTask);
    loadSoftNavigation(new URL(window.location.href), { push: false });
    return true;
  };

  // On a guest detail/tab page, refresh the whole page as soon as a migration
  // for that guest completes — its node, hardware/datastore storage refs and
  // related-object links all change, and the poll shouldn't lag behind.
  const maybeRefreshCurrentGuestDetail = (tasks) => {
    const badge = document.querySelector("[data-active-guest-status-badge][data-guest-target]");
    if (!badge) {
      return false;
    }
    // Match on type:vmid (node-agnostic) — a host migration changes the node.
    const active = parseGuestRef(badge.dataset.guestRef || badge.dataset.guestTarget || "");
    if (!active.type || !active.vmid) {
      return false;
    }
    const completedMigrate = tasks.find((task) => {
      if (task.action !== "guest.migrate" || task.status_class !== "completed" || taskWasReloaded(task)) {
        return false;
      }
      const target = task.target_guest || {};
      if (String(target.type || "") !== active.type || String(target.vmid || "") !== active.vmid) {
        return false;
      }
      return Number(task.finished_at_ms || 0) >= renderedAtMs - 300000;
    });
    if (!completedMigrate) {
      return false;
    }
    rememberTaskReload(completedMigrate);
    loadSoftNavigation(new URL(window.location.href), { push: false });
    return true;
  };

  const taskDetailsHtml = (task) => {
    if (task.pending && task.cancel_upload_id) {
      return `
          ${escapeHtml(task.details)}
          <button class="taskbar-page-button" type="button" data-cancel-upload="${escapeHtml(task.cancel_upload_id)}">Cancel</button>
        `;
    }
    return escapeHtml(task.details);
  };

  const taskTargetSortValue = (task) => {
    const target = task.target_guest || {};
    return target.name || target.vmid || task.target || "";
  };

  const taskRenderSignature = (tasks) =>
    JSON.stringify(
      (tasks || []).map((task) => [
        task.id,
        task.name,
        taskTargetSortValue(task),
        task.status,
        task.status_class,
        task.details,
        task.initiator,
        task.queued_for,
        task.started_at,
        task.started_at_ms,
        task.finished_at,
        task.finished_at_ms,
        task.server,
        task.cancel_upload_id,
        task.cancelable ? "1" : "0",
        task.retryable ? "1" : "0",
        task.retry_label,
      ])
    );

  const taskRowKey = (task, index = 0) =>
    String(
      task.id ||
        `${task.action || "task"}:${task.name || ""}:${taskTargetSortValue(task)}:${task.started_at || ""}:${index}`
    );

  const taskRowSignature = (task) =>
    JSON.stringify([
      task.name,
      taskTargetSortValue(task),
      task.status,
      task.status_class,
      task.details,
      task.initiator,
      task.queued_for,
      task.started_at,
      task.started_at_ms,
      task.finished_at,
      task.finished_at_ms,
      task.server,
      task.cancel_upload_id,
      task.cancelable ? "1" : "0",
      task.offer_force_stop ? "1" : "0",
      task.retryable ? "1" : "0",
      task.retry_label,
    ]);

  const pendingTaskMatchesLoadedTask = (pendingTask, task) => {
    if (!pendingTask || !task || task.action !== pendingTask.action) {
      return false;
    }
    if (Number(task.started_at_ms || 0) < Number(pendingTask.created_at_ms || 0) - 10000) {
      return false;
    }
    if (pendingTask.target_guest?.vmid || task.target_guest?.vmid) {
      if (
        String(pendingTask.target_guest?.vmid || "") !== String(task.target_guest?.vmid || "") ||
        String(pendingTask.target_guest?.type || "") !== String(task.target_guest?.type || "")
      ) {
        return false;
      }
    }
    const pendingDetails = String(pendingTask.details || "");
    const taskDetails = String(task.details || "");
    if (pendingDetails && pendingDetails !== "-" && taskDetails && taskDetails !== "-") {
      return pendingDetails === taskDetails;
    }
    return true;
  };

  const taskRowHtml = (task, index = 0) => `
      <tr
        data-task-row-key="${escapeHtml(taskRowKey(task, index))}"
        data-task-id="${escapeHtml(task.id || "")}"
        data-task-cancelable="${task.cancelable ? "true" : "false"}"
        data-task-retryable="${task.retryable ? "true" : "false"}"
        data-task-row-signature="${escapeHtml(taskRowSignature(task))}"
      >
        <td data-column="task-name" data-sort-value="${escapeHtml(task.name)}">${escapeHtml(task.name)}</td>
        <td data-column="target" data-sort-value="${escapeHtml(taskTargetSortValue(task))}">${task.target_guest ? renderGuestLabel(task.target_guest) : escapeHtml(task.target)}</td>
        <td data-column="cluster" data-sort-value="${escapeHtml(task.cluster || "-")}">${escapeHtml(task.cluster || "-")}</td>
        <td data-column="status" data-sort-value="${escapeHtml(task.status)}">${
          task.question
            ? `<button type="button" class="task-question-badge" data-task-question="${escapeHtml(task.question.kind)}" data-task-question-id="${escapeHtml(task.id || "")}" data-task-question-payload="${escapeHtml(JSON.stringify(task.question.payload || {}))}">${escapeHtml(task.question.label || "A question — click to answer")}</button>`
            : `<span class="badge ${escapeHtml(task.status_class)}"${task.retryable ? ` title="${escapeHtml(task.retry_label)}; right-click for actions"` : ""}>${escapeHtml(task.status)}</span>`
        }</td>
        <td data-column="details" data-sort-value="${escapeHtml(task.details)}">${taskDetailsHtml(task)}</td>
        <td data-column="initiator" data-sort-value="${escapeHtml(task.initiator)}">${escapeHtml(task.initiator)}</td>
        <td data-column="queued" data-sort-value="${escapeHtml(task.queued_for)}">${escapeHtml(task.queued_for)}</td>
        <td data-column="started" data-sort-value="${escapeHtml(task.started_at_ms || 0)}">${escapeHtml(task.started_at)}</td>
        <td data-column="finished" data-sort-value="${escapeHtml(task.finished_at_ms || 0)}">${escapeHtml(task.finished_at)}</td>
        <td data-column="server" data-sort-value="${escapeHtml(task.server)}">${escapeHtml(task.server)}</td>
      </tr>
    `;

  const applyTaskColumnOrderToRow = (row) => {
    const order = Array.from(recentTasks.querySelectorAll("thead th[data-column]"))
      .map((header) => header.dataset.column)
      .filter(Boolean);
    if (!order.length) {
      return row;
    }
    const cellsByColumn = new Map(
      Array.from(row.children)
        .filter((cell) => cell.dataset.column)
        .map((cell) => [cell.dataset.column, cell])
    );
    order.forEach((column) => {
      const cell = cellsByColumn.get(column);
      if (cell) {
        row.appendChild(cell);
      }
    });
    return row;
  };

  const updateTaskRow = (row, task, index) => {
    const template = document.createElement("template");
    template.innerHTML = taskRowHtml(task, index).trim();
    const nextRow = template.content.firstElementChild;
    row.dataset.taskId = nextRow.dataset.taskId || "";
    row.dataset.taskCancelable = nextRow.dataset.taskCancelable || "false";
    row.dataset.taskRetryable = nextRow.dataset.taskRetryable || "false";
    row.dataset.taskRowSignature = nextRow.dataset.taskRowSignature || "";
    row.replaceChildren(...Array.from(nextRow.children));
    applyTaskColumnOrderToRow(row);
  };

  const buildTaskRow = (task, index) => {
    const template = document.createElement("template");
    template.innerHTML = taskRowHtml(task, index).trim();
    return applyTaskColumnOrderToRow(template.content.firstElementChild);
  };

  const renderTaskBody = (tasks) => {
    const existingRows = new Map(
      Array.from(rows.querySelectorAll("[data-task-row-key]")).map((row) => [row.dataset.taskRowKey, row])
    );
    const wantedKeys = new Set();
    let cursor = rows.firstElementChild;
    tasks.forEach((task, index) => {
      const key = taskRowKey(task, index);
      const signature = taskRowSignature(task);
      wantedKeys.add(key);
      let row = existingRows.get(key);
      if (!row) {
        row = buildTaskRow(task, index);
      } else if (row.dataset.taskRowSignature !== signature) {
        updateTaskRow(row, task, index);
      }
      if (row !== cursor) {
        rows.insertBefore(row, cursor);
      }
      cursor = row.nextElementSibling;
    });
    existingRows.forEach((row, key) => {
      if (!wantedKeys.has(key)) {
        row.remove();
      }
    });
    rows.querySelectorAll("tr:not([data-task-row-key])").forEach((row) => {
      row.remove();
    });
  };

  const renderTaskRows = (tasks) => {
    if (!rows) {
      return;
    }

    const selectedCluster = clusterFilter?.value || "";
    const matchesSelectedCluster = (task) =>
      !selectedCluster || !task.cluster_key || task.cluster_key === selectedCluster;
    const visiblePendingTasks = pendingTasksForSelectedCluster();
    const visibleTasks =
      taskPage === 0
        ? tasks.filter(
            (task) =>
              matchesSelectedCluster(task) &&
              !visiblePendingTasks.some((pendingTask) => pendingTaskMatchesLoadedTask(pendingTask, task))
          )
        : tasks;
    const mergedTasks = taskPage === 0 ? [...visiblePendingTasks, ...visibleTasks] : visibleTasks;
    const nextSignature = `${taskPage}:${taskRenderSignature(mergedTasks)}`;
    if (nextSignature === renderedTaskSignature) {
      return;
    }
    renderedTaskSignature = nextSignature;
    if (!visibleTasks.length) {
      if (mergedTasks.length) {
        renderTaskBody(mergedTasks);
        return;
      }
      rows.innerHTML = '<tr><td colspan="10" class="empty-state">No recent tasks.</td></tr>';
      return;
    }

    renderTaskBody(mergedTasks);
  };

  // An unanswered question must be visible even when the taskbar is collapsed,
  // otherwise a half-finished destructive operation can sit unnoticed forever.
  const attention = recentTasks.querySelector("[data-task-attention]");
  const attentionCount = recentTasks.querySelector("[data-task-attention-count]");
  const updateAttention = (pending) => {
    if (!attention) return;
    const count = Number(pending || 0);
    attention.hidden = count <= 0;
    if (attentionCount) {
      attentionCount.textContent = String(count);
    }
  };
  attention?.addEventListener("click", () => {
    // Expand the taskbar so the pinned question rows are actually reachable.
    const appShell = document.querySelector(".app-shell");
    if (appShell?.classList.contains("tasks-collapsed")) {
      document.querySelector("[data-taskbar-toggle]")?.click();
    }
    recentTasks.querySelector("[data-task-question]")?.focus();
  });

  const updateTaskControls = (data) => {
    taskPage = data.page || 0;
    recentTasks.dataset.taskPage = String(taskPage);
    updateAttention(data.questions_pending);

    if (previousButton) {
      previousButton.disabled = !data.has_previous;
    }
    if (nextButton) {
      nextButton.disabled = !data.has_next;
    }
    if (pageLabel) {
      const pendingCount = taskPage === 0 ? pendingTasksForSelectedCluster().length : 0;
      const total = (data.total || 0) + pendingCount;
      const startIndex = data.start_index || (total ? 1 : 0);
      const endIndex = Math.min(total, (data.end_index || 0) + pendingCount);
      pageLabel.textContent = total ? `${startIndex}-${endIndex} of ${total}` : "0 of 0";
    }
  };

  const addPendingTask = (event) => {
    const task = event.detail || {};
    pendingTasks = [task, ...pendingTasks].slice(0, 5);
    if (taskPage === 0) {
      renderTaskRows(lastLoadedTasks);
      updateTaskControls(lastTaskPageData);
    }
  };

  const updatePendingTask = (event) => {
    const patch = event.detail || {};
    pendingTasks = pendingTasks.map((task) => (task.id === patch.id ? { ...task, ...patch } : task));
    if (taskPage === 0) {
      renderTaskRows(lastLoadedTasks);
      updateTaskControls(lastTaskPageData);
    }
  };

  const cancelUpload = (event) => {
    const button = event.target.closest("[data-cancel-upload]");
    if (!button) {
      return;
    }
    const uploadId = button.dataset.cancelUpload || "";
    const xhr = activeUploads.get(uploadId);
    if (xhr) {
      xhr.abort();
    }
  };

  window.addEventListener("pve-helper:pending-task", addPendingTask);
  window.addEventListener("pve-helper:update-pending-task", updatePendingTask);
  if (rows) {
    rows.addEventListener("click", cancelUpload);
  }

  const loadTaskPage = async (page) => {
    if (!tasksUrl) {
      return;
    }

    const normalizedPage = Math.max(0, page);
    if (loadingTasks) {
      pendingLoadPage = normalizedPage;
      return;
    }
    loadingTasks = true;
    try {
      const url = new URL(tasksUrl, window.location.origin);
      url.searchParams.set("page", String(normalizedPage));
      if (clusterFilter?.value) {
        url.searchParams.set("cluster", clusterFilter.value);
      }
      if (normalizedPage === 0) {
        window.dispatchEvent(new CustomEvent(recentTasksRefreshEvent));
      }
      const response = await fetch(url, {
        headers: {
          Accept: "application/json",
        },
      });
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      const loadedTasks = data.tasks || [];
      if (pendingTasks.length) {
        pendingTasks = pendingTasks.filter((pendingTask) => {
          if (pendingTask.pending_kind === "guest") {
            return !loadedTasks.some((task) => pendingTaskMatchesLoadedTask(pendingTask, task));
          }
          // Every file action, not only the two upload ones. The old list meant
          // a rename, move, copy, trash, restore, purge or inflate left its
          // optimistic row behind forever, stuck at whatever status the client
          // last guessed — which is how Recent Tasks came to say "Running" for
          // work Audit had already recorded as finished. Paths are not compared:
          // a rename or move deliberately reports a different path than the one
          // the pending row was created with.
          return !loadedTasks.some(
            (task) =>
              task.kind === "file" &&
              task.storage_id === pendingTask.target &&
              Number(task.started_at_ms || 0) >= Number(pendingTask.created_at_ms || 0) - 5000
          );
        });
      }
      let previousTaskStatuses = new Map();
      if (normalizedPage === 0) {
        previousTaskStatuses = taskStatusesById;
        taskStatusesById = new Map(loadedTasks.map((task) => [task.id, task.status_class]));
      }
      lastLoadedTasks = loadedTasks;
      lastTaskPageData = data;
      renderTaskRows(loadedTasks);
      updateTaskControls(data);
      if (normalizedPage === 0) {
        applyGuestStatusHintsFromTasks(loadedTasks, previousTaskStatuses);
        refreshGuestStateAfterTaskTransitions(loadedTasks, previousTaskStatuses);
      }
      if (
        maybeRefreshCurrentStorageBrowser(loadedTasks) ||
        maybeRefreshTagInventory(loadedTasks) ||
        maybeRefreshStorageCatalogView(loadedTasks) ||
        maybeRefreshCurrentGuestInventory(loadedTasks) ||
        maybeRefreshCurrentGuestDetail(loadedTasks)
      ) {
        return;
      }
      if (maybeRefreshSnapshotState(loadedTasks) || maybeRefreshBackupState(loadedTasks)) {
        return;
      }
    } catch (_error) {
      // Recent task refresh is best effort; the server-rendered rows remain usable.
    } finally {
      loadingTasks = false;
      if (pendingLoadPage !== null) {
        const nextPage = pendingLoadPage;
        pendingLoadPage = null;
        loadTaskPage(nextPage);
      }
    }
  };

  window.pveHelperRefreshRecentTasks = () => {
    if (taskPage === 0 && document.visibilityState !== "hidden") {
      loadTaskPage(0);
    }
  };

  if (previousButton) {
    previousButton.addEventListener("click", () => {
      loadTaskPage(taskPage - 1);
    });
  }

  clusterFilter?.addEventListener("change", () => {
    taskPage = 0;
    loadTaskPage(0);
  });

  if (nextButton) {
    nextButton.addEventListener("click", () => {
      loadTaskPage(taskPage + 1);
    });
  }

  loadTaskPage(taskPage);

  window.setInterval(
    () => {
      if (taskPage === 0 && document.visibilityState !== "hidden") {
        loadTaskPage(0);
      }
    },
    Number.isFinite(pollMs) ? pollMs : 10000
  );
};

export { initRecentTasks };
