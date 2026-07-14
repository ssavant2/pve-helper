import { openConfirmDialog, openInputDialog } from "./dialogs.js";
import { clearLocalError, showLocalError } from "./feedback.js";
import { loadSoftNavigation } from "./navigation.js";
import { FILE_ACTION_META } from "./scheduling.js";
import {
  activeUploads,
  addPendingRecentTask,
  createIcons,
  escapeHtml,
  registerPageCleanup,
  taskDateLabel,
  updatePendingRecentTask,
} from "./shell.js";

const createPendingFileTask = (form) => {
  const now = Date.now();
  const kind = form.dataset.actionKind || "";
  const meta = FILE_ACTION_META[kind] || { action: `file.${kind}`, name: "File action" };
  const manager = form.closest("[data-storage-file-manager]");
  const storageId = manager?.dataset.storageId || "-";
  const path =
    form.querySelector("[data-selected-path-input]")?.value ||
    form.querySelector('input[name="path"]')?.value ||
    form.dataset.fileName ||
    "-";
  return {
    id: `pending-file-${now}-${Math.random().toString(36).slice(2)}`,
    kind: "file",
    pending: true,
    pending_kind: "file",
    action: meta.action,
    name: meta.name,
    target: storageId,
    target_guest: null,
    status: "Starting",
    status_class: "queued",
    details: path,
    initiator: "-",
    queued_for: "-",
    started_at: taskDateLabel(new Date(now)),
    started_at_ms: now,
    finished_at: "-",
    finished_at_ms: 0,
    server: storageId,
    created_at_ms: now,
  };
};

// Same general flow as guest actions: instant Recent Tasks row, run via fetch
// (no navigation), update the row, then soft-refresh the file browser.
const runFileActionForm = async (form) => {
  clearLocalError(form);
  const pending = createPendingFileTask(form);
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
      fail((payload.errors || [`File action failed: HTTP ${response.status}.`]).join("; "));
      return;
    }
    settled = true;
    updatePendingRecentTask({ id: pending.id, status: "Running", status_class: "running" });
    window.pveHelperRefreshRecentTasks?.();
    loadSoftNavigation(new URL(window.location.href), { push: false });
  } catch (_error) {
    fail("Network error");
  }
};

const completeConfirmedFileAction = async (form, { requiresRiskConfirmation, riskMessage }) => {
  const basicInput = form.querySelector('input[name="confirm_basic"]');
  const riskInput = form.querySelector('input[name="confirm_risk"]');
  if (
    requiresRiskConfirmation &&
    riskMessage &&
    !(await openConfirmDialog({
      title: "Are you sure?",
      body: `<p>${escapeHtml(riskMessage)}</p><p>Are you completely sure?</p>`,
      confirmLabel: "Proceed",
      danger: true,
    }))
  ) {
    return;
  }
  if (basicInput) {
    basicInput.value = "yes";
  }
  if (riskInput && requiresRiskConfirmation) {
    riskInput.value = "yes";
  }
  form.dataset.confirmed = "true";
  runFileActionForm(form);
};

const _openMovePicker = (form, options) => {
  const manager = form.closest("[data-storage-file-manager]");
  const dialog = manager?.querySelector("[data-move-picker]");
  const moveInput = form.querySelector("[data-move-input]");
  if (!dialog || !moveInput || typeof dialog.showModal !== "function") {
    return false;
  }

  const targetButtons = Array.from(dialog.querySelectorAll("[data-move-target]"));
  const moveNodes = Array.from(dialog.querySelectorAll("[data-move-node]"));
  const moveToggles = Array.from(dialog.querySelectorAll("[data-move-toggle]"));
  const submitButton = dialog.querySelector("[data-move-picker-submit]");
  const cancelButton = dialog.querySelector("[data-move-picker-cancel]");
  const selectionLabel = dialog.querySelector("[data-move-picker-selection]");
  let selectedTarget = "";
  let selectedLabel = "";

  const moveNodePath = (node) => node.dataset.movePath || "";
  const moveNodeDepth = (node) => Number.parseInt(node.dataset.moveDepth || "0", 10);
  const moveNodeExpanded = (node) => node.dataset.moveExpanded === "true";
  const moveChildPrefix = (path) => (path ? `${path}/` : "");
  const isMoveNodeVisibleByTreeState = (node) => {
    const path = moveNodePath(node);
    const ancestors = moveNodes.filter((candidate) => {
      const candidatePath = moveNodePath(candidate);
      if (candidate === node || moveNodeDepth(candidate) >= moveNodeDepth(node)) {
        return false;
      }
      if (!candidatePath) {
        return true;
      }
      return path.startsWith(moveChildPrefix(candidatePath));
    });
    return ancestors.every((ancestor) => moveNodeExpanded(ancestor));
  };

  const updateMoveTree = () => {
    moveNodes.forEach((node) => {
      node.hidden = !isMoveNodeVisibleByTreeState(node);
      const toggle = node.querySelector("[data-move-toggle]");
      if (toggle) {
        const expanded = moveNodeExpanded(node);
        toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
        const icon = toggle.querySelector("svg");
        if (icon) {
          icon.outerHTML = `<i data-lucide="${expanded ? "chevron-down" : "chevron-right"}" aria-hidden="true"></i>`;
        }
      }
    });
    createIcons();
  };

  const setTarget = (button) => {
    selectedTarget = button.dataset.moveTarget || "";
    selectedLabel = button.dataset.moveTargetLabel || selectedTarget || "Root";
    targetButtons.forEach((targetButton) => {
      targetButton.classList.toggle("selected", targetButton === button);
    });
    if (selectionLabel) {
      selectionLabel.textContent = selectedLabel;
    }
    if (submitButton) {
      submitButton.disabled = !selectedTarget;
    }
  };

  targetButtons.forEach((button) => {
    button.onclick = () => setTarget(button);
    button.classList.remove("selected");
  });
  moveToggles.forEach((toggle) => {
    toggle.onclick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      const node = toggle.closest("[data-move-node]");
      if (!node) {
        return;
      }
      node.dataset.moveExpanded = moveNodeExpanded(node) ? "false" : "true";
      updateMoveTree();
    };
  });
  if (selectionLabel) {
    selectionLabel.textContent = "-";
  }
  if (submitButton) {
    submitButton.disabled = true;
    submitButton.onclick = () => {
      if (!selectedTarget) {
        return;
      }
      // Selecting a destination and clicking the picker's own submit IS the
      // confirmation; completeConfirmedFileAction still gates risky moves.
      moveInput.value = selectedTarget;
      dialog.close();
      completeConfirmedFileAction(form, options);
    };
  }
  if (cancelButton) {
    cancelButton.onclick = () => dialog.close();
  }
  updateMoveTree();
  dialog.showModal();
  return true;
};

const openDestPicker = (form, mode, options) => {
  const manager = form.closest("[data-storage-file-manager]");
  const dialog = manager?.querySelector("[data-dest-picker]");
  if (!dialog || typeof dialog.showModal !== "function") {
    return false;
  }
  const storageInput = form.querySelector("[data-dest-storage]");
  const dirInput = form.querySelector("[data-dest-directory]");
  const nameInput = form.querySelector("[data-dest-name]");
  const storageSelect = dialog.querySelector("[data-dest-picker-storage]");
  const folderSelect = dialog.querySelector("[data-dest-picker-folder]");
  const newFolderField = dialog.querySelector("[data-dest-picker-folder-new]");
  const nameRow = dialog.querySelector("[data-dest-picker-name-row]");
  const nameField = dialog.querySelector("[data-dest-picker-name]");
  const title = dialog.querySelector("[data-dest-title]");
  const submit = dialog.querySelector("[data-dest-picker-submit]");
  const cancel = dialog.querySelector("[data-dest-cancel]");
  const selection = dialog.querySelector("[data-dest-picker-selection]");
  const isCopy = mode === "copy";
  const sourceName = (form.querySelector("[data-selected-path-input]")?.value || "").split("/").pop() || "";
  clearLocalError(dialog);

  if (title) title.textContent = isCopy ? "Copy To" : "Move To";
  if (storageSelect && manager?.dataset.storageId) storageSelect.value = manager.dataset.storageId;
  if (nameRow) nameRow.hidden = !isCopy;
  if (nameField) nameField.value = sourceName;
  if (newFolderField) newFolderField.value = "";

  // Effective destination folder: the picked folder plus any new subfolder.
  const cleanDir = () => {
    const base = (folderSelect?.value || "").trim().replace(/^\/+|\/+$/g, "");
    const extra = (newFolderField?.value || "").trim().replace(/^\/+|\/+$/g, "");
    return [base, extra].filter(Boolean).join("/");
  };
  const refresh = () => {
    const namePart = isCopy ? ` / ${(nameField?.value || sourceName).trim()}` : "";
    if (selection) selection.textContent = `→ [${storageSelect?.value || ""}] ${cleanDir() || "/"}${namePart}`;
  };

  // Populate the folder dropdown from the chosen storage's known directories.
  const loadFolders = (preferredPath) => {
    if (!folderSelect) return;
    const option = storageSelect?.selectedOptions?.[0];
    const url = option?.dataset?.foldersUrl;
    folderSelect.innerHTML = '<option value="">(storage root)</option>';
    refresh();
    if (!url) return;
    fetch(url, { headers: { "X-Requested-With": "XMLHttpRequest" } })
      .then((response) => (response.ok ? response.json() : { folders: [] }))
      .then((data) => {
        (data.folders || []).forEach((path) => {
          const opt = document.createElement("option");
          opt.value = path;
          opt.textContent = path;
          folderSelect.appendChild(opt);
        });
        if (preferredPath && (data.folders || []).includes(preferredPath)) {
          folderSelect.value = preferredPath;
        }
        refresh();
      })
      .catch(() => refresh());
  };

  if (storageSelect) {
    storageSelect.oninput = () => loadFolders("");
  }
  if (folderSelect) folderSelect.oninput = refresh;
  if (newFolderField) newFolderField.oninput = refresh;
  if (nameField) nameField.oninput = refresh;
  // Default to the folder the user is currently browsing on the source storage.
  loadFolders(manager?.dataset.currentPath || "");

  submit.onclick = () => {
    const name = (nameField?.value || sourceName).trim();
    if (isCopy && !name) {
      showLocalError(dialog, "Enter a file name for the copy.");
      nameField?.focus();
      return;
    }
    if (storageInput) storageInput.value = storageSelect?.value || "";
    if (dirInput) dirInput.value = cleanDir();
    if (isCopy && nameInput) nameInput.value = name;
    dialog.close();
    // Copy is read-only on the source, so it never needs the destructive-action
    // risk confirmation (e.g. "belongs to a running guest").
    completeConfirmedFileAction(form, isCopy ? { ...options, requiresRiskConfirmation: false } : options);
  };
  if (cancel) cancel.onclick = () => dialog.close();
  dialog.showModal();
  return true;
};

const initConfirmedFileActions = (root = document) => {
  root.querySelectorAll("[data-confirm-file-action]").forEach((form) => {
    if (form.dataset.initialized === "true") {
      return;
    }

    form.dataset.initialized = "true";
    form.addEventListener("submit", async (event) => {
      if (form.dataset.confirmed === "true") {
        return;
      }

      event.preventDefault();
      let actionKind = form.dataset.actionKind || "file-action";
      if (form.querySelector("[data-move-input]")) {
        actionKind = "move";
      } else if (form.querySelector("[data-rename-input]")) {
        actionKind = "rename";
      }
      const fileName = form.dataset.fileName || "this file";
      const currentPath = form.dataset.currentPath || fileName;
      const selectedCount = Number.parseInt(form.dataset.selectedCount || "1", 10);
      const riskMessage = form.dataset.riskMessage || "";
      const requiresRiskConfirmation = form.dataset.requiresRiskConfirmation === "true";
      const confirmationOptions = {
        currentPath,
        riskMessage,
        requiresRiskConfirmation,
        selectedCount,
      };

      if (actionKind === "new-folder") {
        const folderInput = form.querySelector("[data-new-folder-input]");
        const folderName = await openInputDialog({
          title: "New folder",
          label: "Folder name",
          confirmLabel: "Create",
          validate: (value) =>
            value.includes("/") || value.includes("\\") ? "The folder name must not contain path separators." : "",
        });
        if (!folderName) {
          return;
        }
        folderInput.value = folderName;
      } else if (actionKind === "rename") {
        const renameInput = form.querySelector("[data-rename-input]");
        const nextName = await openInputDialog({
          title: "Rename",
          label: `New name for ${fileName}`,
          value: fileName,
          confirmLabel: "Rename",
          validate: (value) =>
            value.includes("/") || value.includes("\\") ? "The new name must not contain path separators." : "",
        });
        if (!nextName || nextName === fileName) {
          return;
        }
        renameInput.value = nextName;
      } else if (actionKind === "move" || actionKind === "copy") {
        if (openDestPicker(form, actionKind, confirmationOptions)) {
          return;
        }
        showLocalError(form, "No destination picker is available on this page.");
        return;
      } else if (actionKind === "inflate") {
        const inflateMode = form.dataset.inflateMode || "full";
        const targetLabel = inflateMode === "metadata" ? "metadata preallocation" : "full preallocation";
        const modeDescription =
          inflateMode === "metadata"
            ? "Metadata preallocation allocates the QCOW2 map without zero-filling the whole virtual disk."
            : "Full preallocation writes out the whole virtual disk.";
        if (
          !(await openConfirmDialog({
            title: "Inflate disk image",
            body: `<p>Inflate <strong>${escapeHtml(currentPath)}</strong> to ${escapeHtml(targetLabel)}?</p><p>${escapeHtml(modeDescription)}</p><p>The related VM/CT must be stopped. This can take a long time and requires enough free storage space.</p>`,
            confirmLabel: "Inflate",
            danger: true,
          }))
        ) {
          return;
        }
      } else if (actionKind === "purge") {
        if (
          !(await openConfirmDialog({
            title: "Permanently delete",
            body: `<p>Permanently delete <strong>${escapeHtml(fileName)}</strong>?</p><p>This cannot be undone.</p>`,
            confirmLabel: "Delete permanently",
            danger: true,
          }))
        ) {
          return;
        }
      } else {
        const subject = selectedCount > 1 ? `${selectedCount} files` : fileName;
        if (
          !(await openConfirmDialog({
            title: "Move to Recycle Bin",
            body: `<p>Move <strong>${escapeHtml(subject)}</strong> to the Recycle Bin?</p>`,
            confirmLabel: "Move to Recycle Bin",
          }))
        ) {
          return;
        }
      }

      completeConfirmedFileAction(form, confirmationOptions);
    });
  });
};

const initStorageFileManagers = (root = document) => {
  root.querySelectorAll("[data-storage-file-manager]").forEach((manager) => {
    if (manager.dataset.initialized === "true") {
      return;
    }

    manager.dataset.initialized = "true";
    const selectedActionForms = Array.from(manager.querySelectorAll("[data-selected-file-action]"));
    const downloadAction = manager.querySelector("[data-file-download-action]");
    const fileFilter = manager.querySelector("[data-file-filter]");
    const folderFilter = manager.querySelector("[data-folder-filter]");
    const folderNodes = Array.from(manager.querySelectorAll("[data-folder-node]"));
    const uploadForms = Array.from(
      manager.querySelectorAll("[data-upload-on-file-select], [data-upload-folder-on-file-select]")
    );
    const selectedRows = new Set();
    const currentRows = () => Array.from(manager.querySelectorAll("[data-file-row]"));

    if (downloadAction) {
      downloadAction.addEventListener("click", (event) => {
        if (downloadAction.getAttribute("aria-disabled") === "true" || downloadAction.href.endsWith("#")) {
          event.preventDefault();
          return;
        }
        if (downloadAction.dataset.downloadPending === "true") {
          event.preventDefault();
          return;
        }
        downloadAction.dataset.downloadPending = "true";
        window.setTimeout(() => {
          delete downloadAction.dataset.downloadPending;
        }, 5000);
      });
    }

    const nodePath = (node) => node.dataset.folderPath || "";
    const nodeDepth = (node) => Number.parseInt(node.dataset.folderDepth || "0", 10);
    const nodeExpanded = (node) => node.dataset.folderExpanded === "true";
    const childPrefix = (path) => (path ? `${path}/` : "");

    const isVisibleByTreeState = (node) => {
      const path = nodePath(node);
      const ancestors = folderNodes.filter((candidate) => {
        const candidatePath = nodePath(candidate);
        if (candidate === node || nodeDepth(candidate) >= nodeDepth(node)) {
          return false;
        }
        if (!candidatePath) {
          return true;
        }
        return path.startsWith(childPrefix(candidatePath));
      });
      return ancestors.every((ancestor) => nodeExpanded(ancestor));
    };

    const updateFolderTree = () => {
      const query = (folderFilter?.value || "").trim().toLowerCase();
      folderNodes.forEach((node) => {
        const matchesFilter = !query || (node.dataset.folderName || "").includes(query);
        node.hidden = !matchesFilter || !isVisibleByTreeState(node);
        const toggle = node.querySelector("[data-folder-toggle]");
        if (toggle) {
          const expanded = nodeExpanded(node);
          toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
          const icon = toggle.querySelector("svg");
          if (icon) {
            icon.outerHTML = `<i data-lucide="${expanded ? "chevron-down" : "chevron-right"}" aria-hidden="true"></i>`;
          }
        }
      });
      createIcons();
    };

    const setDownloadState = (row) => {
      if (!downloadAction) {
        return;
      }

      const downloadUrl = row?.dataset.downloadUrl || "";
      const enabled = Boolean(downloadUrl);
      downloadAction.classList.toggle("disabled", !enabled);
      downloadAction.setAttribute("aria-disabled", enabled ? "false" : "true");
      downloadAction.href = enabled ? downloadUrl : "#";
    };

    const setSelectedPaths = (form, paths) => {
      form.querySelectorAll("[data-selected-path-extra]").forEach((input) => {
        input.remove();
      });
      const primaryInput = form.querySelector("[data-selected-path-input]");
      if (!primaryInput) {
        return;
      }
      primaryInput.value = paths[0] || "";
      let insertAfter = primaryInput;
      paths.slice(1).forEach((path) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = "path";
        input.value = path;
        input.dataset.selectedPathExtra = "true";
        insertAfter.insertAdjacentElement("afterend", input);
        insertAfter = input;
      });
    };

    const selectedList = () => currentRows().filter((row) => selectedRows.has(row));

    const setActionState = () => {
      const storageActionsEnabled = manager.dataset.storageActionsEnabled === "true";
      const selected = selectedList();
      const selectedPaths = selected.map((row) => row.dataset.filePath || "").filter(Boolean);
      const selectedNames = selected.map((row) => row.dataset.fileName || row.dataset.filePath || "this file");
      const selectedPath = selectedPaths[0] || "";
      const selectedName = selected.length === 1 ? selectedNames[0] : `${selected.length} files`;

      selectedActionForms.forEach((form) => {
        const actionKind = form.dataset.actionKind || "";
        const allowMultiple = form.dataset.allowMultiple === "true";
        const inflateMode = form.dataset.inflateMode || "full";
        const riskMessageKey = actionKind === "inflate" ? "inflateRiskMessage" : "riskMessage";
        const riskConfirmationKey =
          actionKind === "inflate" ? "inflateRequiresRiskConfirmation" : "requiresRiskConfirmation";
        const riskMessages = Array.from(
          new Set(
            selected
              .filter((row) => row.dataset[riskConfirmationKey] === "true")
              .map((row) => row.dataset[riskMessageKey] || "")
              .filter(Boolean)
          )
        );
        const needsSingleSelection = !allowMultiple || actionKind === "rename" || actionKind === "inflate";
        const isActionableForForm = (row) => {
          if (actionKind === "trash") {
            return row.dataset.canTrash === "true";
          }
          if (actionKind === "rename") {
            return row.dataset.entryType === "file" && row.dataset.canRename === "true";
          }
          if (actionKind === "copy") {
            // Copy is read-only on the source, so it is safe for any file
            // (incl. a referenced/in-use disk).
            return row.dataset.entryType === "file";
          }
          if (actionKind === "move") {
            return row.dataset.entryType === "file" && row.dataset.canAction === "true";
          }
          return row.dataset.entryType === "file" && row.dataset.canAction === "true";
        };
        const selectedActionable = selected.length > 0 && selected.every(isActionableForForm);
        const hasValidSelection = needsSingleSelection
          ? selected.length === 1 && selectedActionable
          : selected.length > 0 && selectedActionable;
        const canInflate =
          selected.length === 1 &&
          (inflateMode === "metadata"
            ? selected[0]?.dataset.canInflateMetadata === "true"
            : selected[0]?.dataset.canInflateFull === "true");
        const enabled =
          actionKind === "inflate"
            ? Boolean(storageActionsEnabled && canInflate)
            : Boolean(storageActionsEnabled && hasValidSelection);
        const button = form.querySelector("[data-selected-file-button], [data-inflate-file-button]");
        if (button) {
          button.disabled = !enabled;
        }
        form.dataset.fileName = selectedName;
        form.dataset.currentPath = selectedPath;
        form.dataset.selectedCount = String(selected.length);
        form.dataset.riskMessage = riskMessages.join("\n");
        form.dataset.requiresRiskConfirmation = riskMessages.length ? "true" : "false";
        setSelectedPaths(form, selectedPaths);
      });
    };

    const syncSelectionState = () => {
      currentRows().forEach((item) => {
        const selected = selectedRows.has(item);
        item.classList.toggle("selected", selected);
        const checkbox = item.querySelector("[data-file-select]");
        if (checkbox) {
          checkbox.checked = selected;
        }
      });
      const selected = selectedList();
      setDownloadState(selected.length === 1 ? selected[0] : null);
      setActionState();
    };

    const selectOnlyRow = (row) => {
      selectedRows.clear();
      selectedRows.add(row);
      syncSelectionState();
    };

    const toggleRow = (row, forceSelected = null) => {
      const shouldSelect = forceSelected ?? !selectedRows.has(row);
      if (shouldSelect) {
        selectedRows.add(row);
      } else {
        selectedRows.delete(row);
      }
      syncSelectionState();
    };

    const clearSelection = () => {
      selectedRows.clear();
      syncSelectionState();
    };

    manager.refreshCurrentFileRows = async () => {
      const tableBody = manager.querySelector(".vs-file-table tbody");
      if (!tableBody) {
        return;
      }

      const url = new URL(window.location.href);
      url.searchParams.set("file_partial", "1");
      url.searchParams.set("file_offset", "0");
      url.searchParams.set("include_parent", "1");
      try {
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
        tableBody.innerHTML = data.rows_html || "";
        selectedRows.clear();
        createIcons();
        syncSelectionState();
      } catch (_error) {
        // The next manual navigation or scan refresh will still reconcile the file list.
      }
    };

    const loadNextFiles = async (link) => {
      const loadRow = link.closest("[data-file-load-more-row]");
      if (!loadRow || link.dataset.loading === "true") {
        return;
      }
      clearLocalError(manager);
      const originalText = link.textContent;
      link.dataset.loading = "true";
      link.textContent = "Loading...";
      const url = new URL(link.href, window.location.href);
      url.searchParams.set("file_partial", "1");
      try {
        const response = await fetch(url.href, {
          headers: {
            Accept: "application/json",
            "X-Requested-With": "fetch",
          },
        });
        if (!response.ok) {
          throw new Error("Unable to load more files.");
        }
        const data = await response.json();
        loadRow.insertAdjacentHTML("beforebegin", data.rows_html || "");
        loadRow.remove();
        createIcons();
        syncSelectionState();
      } catch (_error) {
        link.dataset.loading = "false";
        link.textContent = originalText;
        showLocalError(manager, "Could not load more files.");
      }
    };

    manager.addEventListener("click", (event) => {
      const loadMore = event.target.closest("[data-file-load-more]");
      if (loadMore && manager.contains(loadMore)) {
        event.preventDefault();
        loadNextFiles(loadMore);
        return;
      }

      const row = event.target.closest("[data-file-row]");
      if (!row || !manager.contains(row) || event.target.closest("a, button, input, label")) {
        return;
      }
      if (event.ctrlKey || event.metaKey) {
        toggleRow(row);
        return;
      }
      selectOnlyRow(row);
    });

    manager.addEventListener("change", (event) => {
      const checkbox = event.target.closest("[data-file-select]");
      if (!checkbox || !manager.contains(checkbox)) {
        return;
      }
      const row = checkbox.closest("[data-file-row]");
      if (row) {
        toggleRow(row, checkbox.checked);
      }
    });

    if (fileFilter) {
      let searchTimer = null;
      fileFilter.addEventListener("input", () => {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(() => {
          const query = fileFilter.value.trim();
          const url = new URL(window.location.href);
          url.searchParams.delete("file_offset");
          url.searchParams.delete("file_partial");
          if (query) {
            url.searchParams.set("q", query);
          } else {
            url.searchParams.delete("q");
          }
          loadSoftNavigation(url);
        }, 350);
      });
      registerPageCleanup(() => window.clearTimeout(searchTimer));
    }

    if (folderFilter) {
      folderFilter.addEventListener("input", () => {
        updateFolderTree();
      });
    }

    folderNodes.forEach((node) => {
      const toggle = node.querySelector("[data-folder-toggle]");
      if (!toggle) {
        return;
      }
      toggle.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        node.dataset.folderExpanded = nodeExpanded(node) ? "false" : "true";
        updateFolderTree();
      });
    });

    uploadForms.forEach((form) => {
      const input = form.querySelector('input[type="file"]');
      if (!input) {
        return;
      }
      input.addEventListener("change", () => {
        if (!input.files.length) {
          return;
        }
        clearLocalError(form);
        form.querySelectorAll("[data-folder-upload-relative-path]").forEach((item) => {
          item.remove();
        });
        if (form.hasAttribute("data-upload-folder-on-file-select")) {
          Array.from(input.files).forEach((file) => {
            const relativePath = file.webkitRelativePath || file.name;
            const hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "relative_path";
            hidden.value = relativePath;
            hidden.dataset.folderUploadRelativePath = "true";
            form.appendChild(hidden);
          });
        }
        const uploadId = `pending-upload-${Date.now()}`;
        const pendingTask = {
          id: uploadId,
          name: form.hasAttribute("data-upload-folder-on-file-select") ? "Upload folder" : "Upload file",
          target: manager.dataset.storageId || "-",
          status: "Sending",
          status_class: "queued",
          details: form.hasAttribute("data-upload-folder-on-file-select")
            ? `${input.files.length} files`
            : input.files[0]?.name || "-",
          initiator: "-",
          queued_for: "-",
          started_at: taskDateLabel(new Date()),
          finished_at: "-",
          server: manager.dataset.storageId || "-",
          created_at_ms: Date.now(),
          cancel_upload_id: uploadId,
          pending: true,
        };
        addPendingRecentTask(pendingTask);
        const formData = new FormData(form);
        const xhr = new XMLHttpRequest();
        activeUploads.set(uploadId, xhr);
        xhr.upload.addEventListener("progress", (event) => {
          if (!event.lengthComputable) {
            return;
          }
          const percent = Math.max(0, Math.min(100, Math.round((event.loaded * 100) / event.total)));
          updatePendingRecentTask({
            id: uploadId,
            status: percent >= 100 ? "Processing" : "Uploading",
            status_class: percent >= 100 ? "running" : "queued",
            details: percent >= 100 ? "Finalizing upload" : `${percent}%`,
          });
        });
        xhr.addEventListener("load", () => {
          activeUploads.delete(uploadId);
          let payload = {};
          try {
            payload = JSON.parse(xhr.responseText || "{}");
          } catch (_error) {
            payload = {};
          }
          if (xhr.status >= 200 && xhr.status < 300 && payload.ok) {
            updatePendingRecentTask({
              id: uploadId,
              status: "Completed",
              status_class: "completed",
              details: "Upload complete",
              cancel_upload_id: "",
            });
            loadSoftNavigation(new URL(payload.redirect || window.location.href, window.location.origin), {
              push: false,
            });
            return;
          }
          updatePendingRecentTask({
            id: uploadId,
            status: "Failed",
            status_class: "failed",
            details: payload.error || "Upload failed",
            cancel_upload_id: "",
          });
          showLocalError(form, payload.error || "Upload failed.");
        });
        xhr.addEventListener("abort", () => {
          activeUploads.delete(uploadId);
          updatePendingRecentTask({
            id: uploadId,
            status: "Cancelled",
            status_class: "cancelled",
            details: "Upload cancelled",
            cancel_upload_id: "",
          });
        });
        xhr.addEventListener("error", () => {
          activeUploads.delete(uploadId);
          updatePendingRecentTask({
            id: uploadId,
            status: "Failed",
            status_class: "failed",
            details: "Network error",
            cancel_upload_id: "",
          });
        });
        xhr.open("POST", form.action);
        xhr.setRequestHeader("X-PVE-Helper-Async-Upload", "1");
        xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
        xhr.send(formData);
      });
    });

    clearSelection();
    updateFolderTree();
  });
};

export {
  _openMovePicker,
  completeConfirmedFileAction,
  createPendingFileTask,
  initConfirmedFileActions,
  initStorageFileManagers,
  openDestPicker,
  runFileActionForm,
};
