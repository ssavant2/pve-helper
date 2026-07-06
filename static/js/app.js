(() => {
  const themeKey = "pve-helper-theme";
  const guestNameStyleKey = "pve-helper-guest-name-style";
  const taskbarKey = "pve-helper-taskbar-collapsed";
  const softContentSelector = "[data-soft-nav-content]";
  const softStatusSelector = "[data-soft-nav-status]";
  const softTreeSelector = "[data-soft-nav-tree]";
  const recentTasksRefreshEvent = "pve-helper:recent-tasks-refresh";
  const consoleKeepaliveKey = "pve-helper-console-keepalive-minutes";
  const consoleReconnectPrefix = "pve-helper-console-reconnect";
  const consoleLayoutKey = "pve-helper-console-keyboard-layout";

  let activeLabel = "";
  let activeVmOverview = null;
  let activeVmContextRows = [];
  let pageCleanup = [];
  let navigationController = null;
  const activeUploads = new Map();

  const preferredTheme = () => {
    try {
      const storedTheme = localStorage.getItem(themeKey);
      if (storedTheme === "light" || storedTheme === "dark") {
        return storedTheme;
      }
    } catch (_error) {
      return "light";
    }

    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };

  const applyTheme = (theme) => {
    const themeToggle = document.querySelector("[data-theme-toggle]");
    const themeLabels = document.querySelectorAll("[data-theme-label]");

    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    themeLabels.forEach((label) => {
      label.textContent = theme === "dark" ? "Dark" : "Light";
    });
    if (themeToggle) {
      themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
    }
  };

  const applyTaskbarState = (collapsed) => {
    const appShell = document.querySelector(".app-shell");
    const taskbarToggle = document.querySelector("[data-taskbar-toggle]");
    if (!appShell || !taskbarToggle) {
      return;
    }

    appShell.classList.toggle("tasks-collapsed", collapsed);
    taskbarToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    taskbarToggle.setAttribute("aria-label", collapsed ? "Show recent tasks" : "Hide recent tasks");
  };

  const createIcons = () => {
    if (window.lucide) {
      window.lucide.createIcons({
        attrs: {
          "aria-hidden": "true",
        },
      });
    }
  };

  const addPendingRecentTask = (task) => {
    window.dispatchEvent(new CustomEvent("pve-helper:pending-task", { detail: task }));
  };

  const updatePendingRecentTask = (task) => {
    window.dispatchEvent(new CustomEvent("pve-helper:update-pending-task", { detail: task }));
  };

  const guestPowerActions = new Set(["start", "shutdown", "reboot", "stop", "reset"]);

  const expectedGuestStatusFromTask = (task) => {
    if (task.status_class !== "completed") {
      return "";
    }
    const action = String(task.action || "").replace(/^guest\.power\./, "");
    if (!guestPowerActions.has(action)) {
      return "";
    }
    return ["start", "reboot", "reset"].includes(action) ? "running" : "stopped";
  };

  const taskGuestTargetCandidates = (task) => {
    const target = task.target_guest || {};
    const type = String(target.type || "");
    const vmid = target.vmid;
    if (!type || vmid === undefined || vmid === null || vmid === "") {
      return [];
    }
    const base = `${type}:${vmid}`;
    const server = String(task.server || "");
    return server && server !== "-" ? [`${base}@${server}`, base] : [base];
  };

  const applyGuestStatusHintsFromTasks = (tasks, previousTaskStatuses) => {
    let changed = false;
    const touchedTargets = new Set();
    (tasks || []).forEach((task) => {
      const status = expectedGuestStatusFromTask(task);
      if (!status) {
        return;
      }
      const previousStatus = previousTaskStatuses.get(task.id);
      if (!previousStatus || previousStatus === task.status_class) {
        return;
      }
      const targetKey = taskGuestTargetCandidates(task)[0] || "";
      if (targetKey && touchedTargets.has(targetKey)) {
        return;
      }
      if (targetKey) {
        touchedTargets.add(targetKey);
      }
      document.querySelectorAll("[data-vm-overview]").forEach((overview) => {
        const row = taskGuestTargetCandidates(task)
          .map((target) => overview.querySelector(`[data-guest-target="${CSS.escape(target)}"]`))
          .find(Boolean);
        if (!row || row.dataset.guestStatus === status) {
          return;
        }
        updateVmRowStatus(row, {
          target: row.dataset.guestTarget || "",
          status,
          state_label: status === "running" ? "Powered On" : "Powered Off",
        });
        changed = true;
      });
    });
    if (changed) {
      createIcons();
    }
    return changed;
  };

  const taskDateLabel = (date) => {
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };

  const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (char) => {
      const entities = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      };
      return entities[char];
    });

  const registerPageCleanup = (cleanup) => {
    pageCleanup.push(cleanup);
  };

  const runPageCleanup = () => {
    pageCleanup.forEach((cleanup) => {
      cleanup();
    });
    pageCleanup = [];
  };

  const treeStateKey = (moduleName) => `pve-helper-tree-${moduleName}-collapsed`;

  const applyTreeModuleState = (module, collapsed) => {
    const toggle = module.querySelector("[data-tree-toggle]");
    const caret = module.querySelector("[data-tree-caret]");
    module.classList.toggle("collapsed", collapsed);
    module.classList.toggle("expanded", !collapsed);
    if (toggle) {
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      toggle.setAttribute("aria-label", `${collapsed ? "Expand" : "Collapse"} ${module.dataset.treeModule}`);
    }
    if (caret) {
      caret.textContent = collapsed ? ">" : "v";
    }
  };

  const initThemeToggle = () => {
    const themeToggle = document.querySelector("[data-theme-toggle]");
    if (!themeToggle || themeToggle.dataset.initialized === "true") {
      return;
    }

    themeToggle.dataset.initialized = "true";
    themeToggle.addEventListener("click", () => {
      const currentTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      const nextTheme = currentTheme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(themeKey, nextTheme);
      } catch (_error) {
        // Theme persistence is optional; the UI still updates for this page.
      }
      applyTheme(nextTheme);
    });
  };

  const preferredGuestNameStyle = () => {
    try {
      return localStorage.getItem(guestNameStyleKey) === "name-only" ? "name-only" : "id-name";
    } catch (_error) {
      return "id-name";
    }
  };

  const applyGuestNameStyle = (style) => {
    const value = style === "name-only" ? "name-only" : "id-name";
    document.documentElement.dataset.guestNameStyle = value;
    const showing = value === "id-name";
    document.querySelectorAll("[data-guest-id-label]").forEach((label) => {
      label.textContent = showing ? "IDs on" : "IDs off";
    });
    const toggle = document.querySelector("[data-guest-id-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-pressed", showing ? "true" : "false");
      toggle.setAttribute("aria-label", showing ? "Hide VM/CT IDs" : "Show VM/CT IDs");
    }
  };

  const initGuestNameToggle = () => {
    const toggle = document.querySelector("[data-guest-id-toggle]");
    if (!toggle || toggle.dataset.initialized === "true") {
      return;
    }
    toggle.dataset.initialized = "true";
    toggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.guestNameStyle === "name-only" ? "id-name" : "name-only";
      try {
        localStorage.setItem(guestNameStyleKey, next);
      } catch (_error) {
        // Guest-name persistence is optional; the UI still updates for this page.
      }
      applyGuestNameStyle(next);
    });
  };

  // Client twin of the {% guest_label %} server tag: builds the identical
  // markup from a JSON {type, vmid, name} so the app-wide VMID toggle applies
  // to JSON-refreshed rows (Recent Tasks / Latest Runs) too.
  const renderGuestLabel = (guest) => {
    const vmid = guest && guest.vmid != null ? String(guest.vmid) : "";
    const name = guest?.name ? String(guest.name) : "";
    const full = Boolean(vmid && name);
    let inner = "";
    if (vmid) {
      inner += `<span class="guest-vmid">${escapeHtml(vmid)}</span>`;
    }
    if (name) {
      inner += `<span class="guest-name">${escapeHtml(name)}</span>`;
    } else if (!vmid) {
      inner += `<span class="guest-name">?</span>`;
    }
    const title = full ? `${vmid} (${name})` : name || vmid || "?";
    return `<span class="guest-label${full ? " guest-label--full" : ""}" title="${escapeHtml(title)}">${inner}</span>`;
  };

  const initTaskbarToggle = () => {
    const appShell = document.querySelector(".app-shell");
    const taskbarToggle = document.querySelector("[data-taskbar-toggle]");
    if (!appShell || !taskbarToggle || taskbarToggle.dataset.initialized === "true") {
      return;
    }

    taskbarToggle.dataset.initialized = "true";
    taskbarToggle.addEventListener("click", () => {
      const collapsed = !appShell.classList.contains("tasks-collapsed");
      try {
        localStorage.setItem(taskbarKey, collapsed ? "true" : "false");
      } catch (_error) {
        // The visual state still changes even when localStorage is unavailable.
      }
      applyTaskbarState(collapsed);
    });
  };

  const initTreeModules = (root = document) => {
    root.querySelectorAll("[data-tree-module]").forEach((module) => {
      const moduleName = module.dataset.treeModule;
      const toggle = module.querySelector("[data-tree-toggle]");
      if (!moduleName || !toggle) {
        return;
      }

      try {
        applyTreeModuleState(module, localStorage.getItem(treeStateKey(moduleName)) === "true");
      } catch (_error) {
        applyTreeModuleState(module, false);
      }

      if (toggle.dataset.initialized === "true") {
        return;
      }

      toggle.dataset.initialized = "true";
      toggle.addEventListener("click", () => {
        const collapsed = !module.classList.contains("collapsed");
        try {
          localStorage.setItem(treeStateKey(moduleName), collapsed ? "true" : "false");
        } catch (_error) {
          // The tree still expands/collapses even when persistence is unavailable.
        }
        applyTreeModuleState(module, collapsed);
      });
    });
  };

  const initAutoSubmitForms = (root = document) => {
    root.querySelectorAll("[data-auto-submit-form]").forEach((form) => {
      if (form.dataset.initialized === "true") {
        return;
      }

      form.dataset.initialized = "true";
      form.querySelectorAll("[data-auto-submit-control]").forEach((control) => {
        control.addEventListener("change", () => {
          if (form.reportValidity && !form.reportValidity()) {
            return;
          }

          if (form.requestSubmit) {
            form.requestSubmit();
            return;
          }
          form.submit();
        });
      });
    });
  };

  const initScanActions = (root = document) => {
    root.querySelectorAll("[data-scan-action]").forEach((form) => {
      if (form.dataset.initialized === "true") {
        return;
      }

      form.dataset.initialized = "true";
      const scanButton = form.querySelector("[data-scan-button]");
      const scanButtonLabel = form.querySelector("[data-scan-button-label]");
      const scanSpinner = form.querySelector("[data-scan-spinner]");
      const scanStatusUrl = form.dataset.scanStatusUrl;
      const scanPollMs = Number.parseInt(form.dataset.scanPollMs || "5000", 10);
      let scanWasActive = scanButton ? scanButton.disabled : false;

      const setScanButtonState = (active, label) => {
        if (!scanButton || !scanButtonLabel) {
          return;
        }

        scanButton.disabled = Boolean(active);
        scanButton.classList.toggle("loading", Boolean(active));
        scanButtonLabel.textContent = label || (active ? "Scanning" : "Start scan");
        if (scanSpinner) {
          scanSpinner.hidden = !active;
        }
      };

      const loadScanStatus = async () => {
        if (!scanStatusUrl) {
          return;
        }

        try {
          const response = await fetch(scanStatusUrl, {
            headers: {
              Accept: "application/json",
            },
          });
          if (!response.ok) {
            return;
          }
          const data = await response.json();
          if (scanWasActive && !data.active) {
            window.location.reload();
            return;
          }
          scanWasActive = Boolean(data.active);
          setScanButtonState(data.active, data.button_label);
        } catch (_error) {
          // The current button state remains usable if the status poll fails.
        }
      };

      form.addEventListener("submit", () => {
        scanWasActive = true;
        setScanButtonState(true, "Scan queued");
      });

      const intervalId = window.setInterval(
        () => {
          if (document.visibilityState !== "hidden") {
            loadScanStatus();
          }
        },
        Number.isFinite(scanPollMs) ? scanPollMs : 5000
      );
      registerPageCleanup(() => window.clearInterval(intervalId));
    });
  };

  const completeConfirmedFileAction = (form, { requiresRiskConfirmation, riskMessage }) => {
    const basicInput = form.querySelector('input[name="confirm_basic"]');
    const riskInput = form.querySelector('input[name="confirm_risk"]');
    if (requiresRiskConfirmation && riskMessage && !window.confirm(`${riskMessage}\n\nAre you completely sure?`)) {
      return;
    }
    if (basicInput) {
      basicInput.value = "yes";
    }
    if (riskInput && requiresRiskConfirmation) {
      riskInput.value = "yes";
    }
    form.dataset.confirmed = "true";
    form.submit();
  };

  const openMovePicker = (form, options) => {
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
        const subject = options.selectedCount > 1 ? `${options.selectedCount} files` : options.currentPath;
        if (!window.confirm(`Move ${subject} to ${selectedLabel}?`)) {
          return;
        }
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
    const folderField = dialog.querySelector("[data-dest-picker-folder]");
    const nameRow = dialog.querySelector("[data-dest-picker-name-row]");
    const nameField = dialog.querySelector("[data-dest-picker-name]");
    const title = dialog.querySelector("[data-dest-title]");
    const submit = dialog.querySelector("[data-dest-picker-submit]");
    const cancel = dialog.querySelector("[data-dest-cancel]");
    const selection = dialog.querySelector("[data-dest-picker-selection]");
    const isCopy = mode === "copy";
    const sourceName = (form.querySelector("[data-selected-path-input]")?.value || "").split("/").pop() || "";

    if (title) title.textContent = isCopy ? "Copy To" : "Move To";
    if (storageSelect && manager?.dataset.storageId) storageSelect.value = manager.dataset.storageId;
    if (folderField) folderField.value = manager?.dataset.currentPath || "";
    if (nameRow) nameRow.hidden = !isCopy;
    if (nameField) nameField.value = sourceName;

    const cleanDir = () => (folderField?.value || "").trim().replace(/^\/+|\/+$/g, "");
    const refresh = () => {
      const namePart = isCopy ? ` / ${(nameField?.value || sourceName).trim()}` : "";
      if (selection) selection.textContent = `→ [${storageSelect?.value || ""}] ${cleanDir() || "/"}${namePart}`;
    };
    if (storageSelect) storageSelect.oninput = refresh;
    if (folderField) folderField.oninput = refresh;
    if (nameField) nameField.oninput = refresh;
    refresh();

    submit.onclick = () => {
      const name = (nameField?.value || sourceName).trim();
      if (isCopy && !name) {
        window.alert("Enter a file name for the copy.");
        return;
      }
      if (storageInput) storageInput.value = storageSelect?.value || "";
      if (dirInput) dirInput.value = cleanDir();
      if (isCopy && nameInput) nameInput.value = name;
      dialog.close();
      completeConfirmedFileAction(form, options);
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
      form.addEventListener("submit", (event) => {
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
          const folderName = window.prompt("New folder name");
          if (!folderName) {
            return;
          }
          if (folderName.includes("/") || folderName.includes("\\")) {
            window.alert("The folder name must not contain path separators.");
            return;
          }
          folderInput.value = folderName;
          if (!window.confirm(`Create folder ${folderName}?`)) {
            return;
          }
        } else if (actionKind === "rename") {
          const renameInput = form.querySelector("[data-rename-input]");
          const nextName = window.prompt(`New name for ${fileName}`, fileName);
          if (!nextName || nextName === fileName) {
            return;
          }
          if (nextName.includes("/") || nextName.includes("\\")) {
            window.alert("The new name must not contain path separators.");
            return;
          }
          renameInput.value = nextName;
          if (!window.confirm(`Rename ${fileName} to ${nextName}?`)) {
            return;
          }
        } else if (actionKind === "move" || actionKind === "copy") {
          if (openDestPicker(form, actionKind, confirmationOptions)) {
            return;
          }
          window.alert("No destination picker is available on this page.");
          return;
        } else if (actionKind === "inflate") {
          const inflateMode = form.dataset.inflateMode || "full";
          const targetLabel = inflateMode === "metadata" ? "metadata preallocation" : "full preallocation";
          const modeDescription =
            inflateMode === "metadata"
              ? "Metadata preallocation allocates the QCOW2 map without zero-filling the whole virtual disk."
              : "Full preallocation writes out the whole virtual disk.";
          if (
            !window.confirm(
              `Inflate ${currentPath} to ${targetLabel}?\n\n${modeDescription}\n\nThe related VM/CT must be stopped. This can take a long time and requires enough free storage space.`
            )
          ) {
            return;
          }
        } else if (actionKind === "purge") {
          if (!window.confirm(`Permanently delete ${fileName}?\n\nThis cannot be undone.`)) {
            return;
          }
        } else {
          const subject = selectedCount > 1 ? `${selectedCount} files` : fileName;
          if (!window.confirm(`Move ${subject} to the Recycle Bin?`)) {
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

      const loadNextFiles = async (link) => {
        const loadRow = link.closest("[data-file-load-more-row]");
        if (!loadRow || link.dataset.loading === "true") {
          return;
        }
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
          window.alert("Could not load more files.");
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
              window.location.href = payload.redirect || window.location.href;
              return;
            }
            updatePendingRecentTask({
              id: uploadId,
              status: "Failed",
              status_class: "failed",
              details: payload.error || "Upload failed",
              cancel_upload_id: "",
            });
            window.alert(payload.error || "Upload failed.");
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

  const initRecentTasks = () => {
    const recentTasks = document.querySelector("[data-recent-tasks]");
    if (!recentTasks || recentTasks.dataset.initialized === "true") {
      return;
    }

    recentTasks.dataset.initialized = "true";
    const rows = recentTasks.querySelector("[data-task-rows]");
    const previousButton = recentTasks.querySelector("[data-task-prev]");
    const nextButton = recentTasks.querySelector("[data-task-next]");
    const pageLabel = recentTasks.querySelector("[data-task-page-label]");
    const tasksUrl = recentTasks.dataset.tasksUrl;
    const pollMs = Number.parseInt(recentTasks.dataset.taskPollMs || "10000", 10);
    const parsedRenderedAtMs = Date.parse(recentTasks.dataset.taskRenderedAt || "");
    const renderedAtMs = Number.isFinite(parsedRenderedAtMs) ? parsedRenderedAtMs : Date.now();
    let taskPage = Number.parseInt(recentTasks.dataset.taskPage || "0", 10);
    let loadingTasks = false;
    let storageReloadPending = false;
    let pendingTasks = [];
    let lastLoadedTasks = [];
    let taskStatusesById = new Map();
    let lastTaskPageData = {
      page: taskPage,
      total: 0,
      start_index: 0,
      end_index: 0,
      has_previous: false,
      has_next: false,
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

    const maybeReloadCurrentStorageBrowser = (tasks) => {
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

      storageReloadPending = true;
      rememberTaskReload(completedInflate);
      window.location.reload();
      return true;
    };

    const maybeReloadCurrentSnapshotView = (tasks) => {
      const snapshotView = document.querySelector("[data-guest-snapshots]");
      if (!snapshotView) {
        return false;
      }

      const objectType = snapshotView.dataset.objectType || "";
      const vmid = String(snapshotView.dataset.vmid || "");
      const renderedAtMs = Number(snapshotView.dataset.renderedAtMs || 0);
      const completedSnapshotTask = tasks.find((task) => {
        if (!String(task.action || "").startsWith("guest.snapshot.")) {
          return false;
        }
        if (task.status_class !== "completed") {
          return false;
        }
        const target = task.target_guest || {};
        if (String(target.type || "") !== objectType || String(target.vmid || "") !== vmid) {
          return false;
        }
        if (Number(task.finished_at_ms || 0) <= renderedAtMs) {
          return false;
        }
        return !taskWasReloaded(task);
      });

      if (!completedSnapshotTask) {
        return false;
      }

      rememberTaskReload(completedSnapshotTask);
      window.location.reload();
      return true;
    };

    const maybeReloadCurrentGuestInventory = (tasks) => {
      const overview = document.querySelector("[data-vm-overview]");
      if (!overview) {
        return false;
      }

      const completedInventoryTask = tasks.find((task) => {
        if (!["guest.destroy", "guest.clone.create"].includes(task.action)) {
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
      window.location.reload();
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

    const taskRowHtml = (task) => `
      <tr>
        <td>${escapeHtml(task.name)}</td>
        <td>${task.target_guest ? renderGuestLabel(task.target_guest) : escapeHtml(task.target)}</td>
        <td><span class="badge ${escapeHtml(task.status_class)}">${escapeHtml(task.status)}</span></td>
        <td>${taskDetailsHtml(task)}</td>
        <td>${escapeHtml(task.initiator)}</td>
        <td>${escapeHtml(task.queued_for)}</td>
        <td>${escapeHtml(task.started_at)}</td>
        <td>${escapeHtml(task.finished_at)}</td>
        <td>${escapeHtml(task.server)}</td>
      </tr>
    `;

    const renderTaskRows = (tasks) => {
      if (!rows) {
        return;
      }

      const mergedTasks = taskPage === 0 ? [...pendingTasks, ...tasks] : tasks;
      if (!tasks.length) {
        if (mergedTasks.length) {
          rows.innerHTML = mergedTasks.map(taskRowHtml).join("");
          return;
        }
        rows.innerHTML = '<tr><td colspan="9" class="empty-state">No recent tasks.</td></tr>';
        return;
      }

      rows.innerHTML = mergedTasks.map(taskRowHtml).join("");
    };

    const updateTaskControls = (data) => {
      taskPage = data.page || 0;
      recentTasks.dataset.taskPage = String(taskPage);

      if (previousButton) {
        previousButton.disabled = !data.has_previous;
      }
      if (nextButton) {
        nextButton.disabled = !data.has_next;
      }
      if (pageLabel) {
        const pendingCount = taskPage === 0 ? pendingTasks.length : 0;
        const total = (data.total || 0) + pendingCount;
        const startIndex = data.start_index || (total ? 1 : 0);
        const endIndex = Math.min(total, (data.end_index || 0) + pendingCount);
        pageLabel.textContent = total ? `${startIndex}-${endIndex} of ${total}` : "0 of 0";
      }
    };

    const addPendingTask = (event) => {
      const task = event.detail || {};
      pendingTasks = [task, ...pendingTasks].slice(0, 3);
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
    registerPageCleanup(() => window.removeEventListener("pve-helper:pending-task", addPendingTask));
    registerPageCleanup(() => window.removeEventListener("pve-helper:update-pending-task", updatePendingTask));
    if (rows) {
      registerPageCleanup(() => rows.removeEventListener("click", cancelUpload));
    }

    const loadTaskPage = async (page) => {
      if (!tasksUrl || loadingTasks) {
        return;
      }

      const normalizedPage = Math.max(0, page);
      loadingTasks = true;
      try {
        const url = new URL(tasksUrl, window.location.origin);
        url.searchParams.set("page", String(normalizedPage));
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
          pendingTasks = pendingTasks.filter(
            (pendingTask) =>
              !loadedTasks.some(
                (task) =>
                  (task.action === "file.uploaded" || task.action === "file.folder_uploaded") &&
                  task.storage_id === pendingTask.target &&
                  Number(task.finished_at_ms || 0) >= Number(pendingTask.created_at_ms || 0) - 5000
              )
          );
        }
        let previousTaskStatuses = new Map();
        if (normalizedPage === 0) {
          previousTaskStatuses = taskStatusesById;
          taskStatusesById = new Map(loadedTasks.map((task) => [task.id, task.status_class]));
        }
        if (
          maybeReloadCurrentStorageBrowser(loadedTasks) ||
          maybeReloadCurrentSnapshotView(loadedTasks) ||
          maybeReloadCurrentGuestInventory(loadedTasks)
        ) {
          return;
        }
        lastLoadedTasks = loadedTasks;
        lastTaskPageData = data;
        renderTaskRows(loadedTasks);
        updateTaskControls(data);
        if (normalizedPage === 0) {
          applyGuestStatusHintsFromTasks(loadedTasks, previousTaskStatuses);
        }
      } catch (_error) {
        // Recent task refresh is best effort; the server-rendered rows remain usable.
      } finally {
        loadingTasks = false;
      }
    };

    if (previousButton) {
      previousButton.addEventListener("click", () => {
        loadTaskPage(taskPage - 1);
      });
    }

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

  const initScheduledRuns = (root = document) => {
    root.querySelectorAll("[data-scheduled-runs]").forEach((panel) => {
      if (panel.dataset.initialized === "true") {
        return;
      }

      panel.dataset.initialized = "true";
      const rows = panel.querySelector("[data-scheduled-run-rows]");
      const runsUrl = panel.dataset.scheduledRunsUrl || "";
      const pollMs = Number.parseInt(panel.dataset.scheduledRunsPollMs || "10000", 10);
      const hasRecentTaskbar = Boolean(document.querySelector("[data-recent-tasks]"));
      let loadingRuns = false;

      const runRowHtml = (run) => `
        <tr>
          <td>${escapeHtml(run.planned_for)}</td>
          <td>${escapeHtml(run.task)}</td>
          <td>${run.target_guest ? renderGuestLabel(run.target_guest) : escapeHtml(run.target)}</td>
          <td><span class="badge ${escapeHtml(run.status_class)}">${escapeHtml(run.status)}</span></td>
          <td>${escapeHtml(run.outcome)}</td>
          <td>${escapeHtml(run.started_at)}</td>
          <td>${escapeHtml(run.finished_at)}</td>
          <td>${escapeHtml(run.node)}</td>
          <td>${escapeHtml(run.message)}</td>
        </tr>
      `;

      const renderRuns = (runs) => {
        if (!rows) {
          return;
        }
        if (!runs.length) {
          rows.innerHTML = '<tr><td colspan="9" class="empty-state">No scheduled task runs yet.</td></tr>';
          return;
        }
        rows.innerHTML = runs.map(runRowHtml).join("");
      };

      const loadRuns = async () => {
        if (!runsUrl || loadingRuns) {
          return;
        }

        loadingRuns = true;
        try {
          const response = await fetch(new URL(runsUrl, window.location.origin), {
            headers: {
              Accept: "application/json",
            },
          });
          if (!response.ok) {
            return;
          }
          const data = await response.json();
          renderRuns(data.runs || []);
        } catch (_error) {
          // Latest runs refresh is best effort; the server-rendered rows remain usable.
        } finally {
          loadingRuns = false;
        }
      };

      const refreshWithRecentTasks = () => {
        if (document.visibilityState !== "hidden") {
          loadRuns();
        }
      };
      window.addEventListener(recentTasksRefreshEvent, refreshWithRecentTasks);
      registerPageCleanup(() => window.removeEventListener(recentTasksRefreshEvent, refreshWithRecentTasks));

      if (!hasRecentTaskbar) {
        loadRuns();
        const intervalId = window.setInterval(refreshWithRecentTasks, Number.isFinite(pollMs) ? pollMs : 10000);
        registerPageCleanup(() => window.clearInterval(intervalId));
      }
    });
  };

  const vmOverviewRows = (overview) => Array.from(overview.querySelectorAll("[data-vm-overview-row]"));

  const visibleVmOverviewRows = (overview) => vmOverviewRows(overview).filter((row) => !row.hidden);

  const selectedVmOverviewRows = (overview) =>
    vmOverviewRows(overview).filter((row) => row.querySelector("[data-vm-select]")?.checked);

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
              [row.querySelector("[data-agent-ip-cell]"), guest.ip_label],
              [row.querySelector("[data-agent-status-cell]"), guest.agent],
            ];
            updates.forEach(([cell, value]) => {
              if (!cell || !value) {
                return;
              }
              cell.textContent = value;
              cell.dataset.sortValue = value;
            });
            const extraFilterText = [guest.guest_os, guest.ip_label, guest.agent]
              .filter(Boolean)
              .join(" ")
              .toLowerCase();
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

      loadSnapshotInfo();
    });
  };

  const iconForGuestStatus = (row, status) => {
    if (status === "running") {
      return "play";
    }
    if (row.dataset.guestType === "ct") {
      return "box";
    }
    if (row.dataset.guestTemplate === "true") {
      return "layers";
    }
    return "monitor";
  };

  const updateVmRowStatus = (row, guest) => {
    const previousStatus = row.dataset.guestStatus || "";
    const status = guest.status || "";
    const stateLabel =
      guest.state_label || (status === "running" ? "Powered On" : status === "stopped" ? "Powered Off" : "-");
    row.dataset.guestStatus = status;

    const stateCell = row.querySelector("[data-guest-state-cell]");
    if (stateCell) {
      stateCell.textContent = stateLabel;
      stateCell.dataset.sortValue = stateLabel;
    }

    const statusIcon = row.querySelector("[data-guest-status-icon]");
    if (statusIcon) {
      const iconName = iconForGuestStatus(row, status);
      statusIcon.classList.toggle("running-icon", status === "running");
      statusIcon.title = status || "unknown";
      if (statusIcon.dataset.currentIcon !== iconName) {
        statusIcon.dataset.currentIcon = iconName;
        statusIcon.innerHTML = `<i data-lucide="${iconName}" aria-hidden="true"></i>`;
      }
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
          const liveTargets = new Set((data.guests || []).map((guest) => guest.target || ""));
          (data.guests || []).forEach((guest) => {
            const target = guest.target || "";
            const row = overview.querySelector(`[data-guest-target="${CSS.escape(target)}"]`);
            if (!row) {
              return;
            }
            updateVmRowStatus(row, guest);
          });
          if (data.live_available) {
            vmOverviewRows(overview).forEach((row) => {
              const target = row.dataset.guestTarget || "";
              if (target && !liveTargets.has(target)) {
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
      refresh();
      const intervalId = window.setInterval(refresh, 2500);
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
          String(row.value || "")
            .split("\n")
            .forEach((line, index) => {
              if (index > 0) {
                value.appendChild(document.createElement("br"));
              }
              value.appendChild(document.createTextNode(line));
            });
          wrapper.append(term, value);
          details.insertBefore(wrapper, details.lastElementChild);
        });
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

  const submitVmBulkAction = (overview, action, fields = {}, targetRows = null) => {
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
    if (form.requestSubmit) {
      form.requestSubmit();
    } else {
      form.submit();
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
        if (!/^[A-Za-z0-9_-]+$/.test(snapshotName)) {
          return "Snapshot names can only contain letters, digits, _ and -.";
        }
        submitVmBulkAction(overview, "snapshot", { snapshot_name: snapshotName }, rows);
        return "";
      },
    });
  };

  const openTagsDialog = (overview, rows) => {
    openVmFormDialog({
      title: "Edit Tags",
      summary: selectedGuestSummary(rows),
      submitLabel: "Apply Tags",
      bodyHtml: `
        <label class="form-field">
          <span>Operation</span>
          <select name="tags_mode">
            <option value="add">Add tags</option>
            <option value="remove">Remove tags</option>
            <option value="replace">Replace all tags</option>
          </select>
        </label>
        <label class="form-field">
          <span>Tags</span>
          <input type="text" name="tags_value" autocomplete="off" placeholder="backup-standard veeam-standard">
        </label>
      `,
      onSubmit: (formData) => {
        const mode = String(formData.get("tags_mode") || "").trim();
        const tags = String(formData.get("tags_value") || "").trim();
        if (!["add", "remove", "replace"].includes(mode)) {
          return "Choose a tag operation.";
        }
        if (mode !== "replace" && !tags) {
          return "Enter at least one tag.";
        }
        submitVmBulkAction(overview, "tags", { tags_mode: mode, tags_value: tags }, rows);
        return "";
      },
    });
  };

  const openCloneDialog = (overview, rows) => {
    const row = rows[0];
    const label = row?.dataset.guestLabel || "guest";
    const guestName = row?.dataset.guestName || "";
    const dialog = openVmFormDialog({
      title: "Clone",
      summary: label,
      submitLabel: "Clone",
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
        <label class="form-field form-field-inline">
          <input type="checkbox" name="clone_full" value="1" checked>
          <span>Full clone</span>
        </label>
      `,
      onSubmit: (formData) => {
        const newid = String(formData.get("clone_newid") || "").trim();
        if (!/^[0-9]+$/.test(newid) || Number(newid) <= 0) {
          return "New VMID must be a positive whole number.";
        }
        const name = String(formData.get("clone_name") || "").trim();
        if (!name) {
          return "Name is required.";
        }
        submitVmBulkAction(
          overview,
          "clone",
          {
            clone_newid: newid,
            clone_name: name,
            clone_storage: String(formData.get("clone_storage") || "").trim(),
            clone_full: formData.get("clone_full") === "1" ? "1" : "0",
          },
          rows
        );
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
      if (storageSelect && fullCheckbox) {
        storageSelect.disabled = !fullCheckbox.checked || storageSelect.options.length === 0;
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
        if (idInput) {
          idInput.value = data.nextid || "";
          idInput.disabled = false;
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
    const vmid = target.split(":")[1] || "";
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
    const writable = overview.dataset.vmWriteEnabled === "true";
    const allRunning = contextRows.every((item) => item.dataset.guestStatus === "running");
    const allNotRunning = contextRows.every((item) => item.dataset.guestStatus !== "running");
    const allStopped = contextRows.every((item) => item.dataset.guestStatus === "stopped");
    const allVms = contextRows.every((item) => item.dataset.guestType === "vm");
    const noTemplates = contextRows.every((item) => item.dataset.guestTemplate !== "true");
    const singleSelected = contextRows.length === 1;

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
          <button type="button" data-vm-action="start" ${writable && allNotRunning ? "" : "disabled"}><i data-lucide="play" aria-hidden="true"></i>Power On</button>
          <button type="button" data-vm-action="stop" ${writable && allRunning ? "" : "disabled"}><i data-lucide="square" aria-hidden="true"></i>Power Off</button>
          <button type="button" data-vm-action="reset" ${writable && allRunning && allVms ? "" : "disabled"}><i data-lucide="rotate-ccw" aria-hidden="true"></i>Reset</button>
          <div class="context-menu-separator"></div>
          <button type="button" data-vm-action="shutdown" ${writable && allRunning ? "" : "disabled"}><i data-lucide="power" aria-hidden="true"></i>Shut Down Guest OS</button>
          <button type="button" data-vm-action="reboot" ${writable && allRunning ? "" : "disabled"}><i data-lucide="refresh-cw" aria-hidden="true"></i>Restart Guest OS</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Guest OS <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="open-summary" ${singleSelected ? "" : "disabled"}>Open Summary</button>
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
      <div class="context-menu-separator"></div>
      <button type="button" disabled><i data-lucide="move-right" aria-hidden="true"></i>Migrate...</button>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Template <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="clone" ${singleSelected && writable ? "" : "disabled"}>Clone...</button>
          <button type="button" data-vm-action="template" ${writable && allStopped && allVms && noTemplates ? "" : "disabled"}>Convert to Template</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Tags <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="edit-tags" ${writable ? "" : "disabled"}>Edit Tags...</button>
          <button type="button" disabled>Remove Tags...</button>
        </div>
      </div>
      <div class="context-menu-separator"></div>
      <button type="button" data-vm-action="destroy" class="danger" ${singleSelected && writable && allStopped ? "" : "disabled"}>Remove from Disk...</button>
    `;
    menu.style.left = `${event.clientX}px`;
    menu.style.top = `${event.clientY}px`;
    menu.hidden = false;
    createIcons();
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
      menu.style.left = `${event.clientX}px`;
      menu.style.top = `${event.clientY}px`;
      menu.hidden = false;
    });

    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) {
        menu.hidden = true;
        clearVmContextHighlights();
      }
    });

    menu.addEventListener("click", async (event) => {
      const vmButton = event.target.closest("button[data-vm-action]");
      if (vmButton && activeVmOverview) {
        const targetRows = activeVmContextRows.length ? activeVmContextRows : selectedVmOverviewRows(activeVmOverview);
        const firstRow = targetRows[0];
        const action = vmButton.dataset.vmAction || "";
        if (vmButton.disabled || !firstRow) {
          return;
        }
        if (action === "open-summary") {
          window.location.href = firstRow.dataset.detailUrl || window.location.href;
          return;
        }
        if (action === "edit-hardware") {
          window.location.href =
            firstRow.dataset.editHardwareUrl ||
            firstRow.dataset.editOptionsUrl ||
            firstRow.dataset.detailUrl ||
            window.location.href;
          return;
        }
        if (action === "open-snapshots") {
          window.location.href = firstRow.dataset.snapshotsUrl || window.location.href;
          return;
        }
        if (action === "edit-tags") {
          openTagsDialog(activeVmOverview, targetRows);
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
        if (action === "clone") {
          openCloneDialog(activeVmOverview, targetRows);
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
        if (
          action === "delete-snapshots" &&
          !window.confirm(
            `Delete all snapshots for ${targetRows.length} selected guest${targetRows.length === 1 ? "" : "s"}? This cannot be undone.`
          )
        ) {
          menu.hidden = true;
          clearVmContextHighlights();
          return;
        }
        if (
          ["stop", "reset"].includes(action) &&
          !window.confirm(
            `${action === "reset" ? "Reset" : "Power off"} ${targetRows.length} selected guest${targetRows.length === 1 ? "" : "s"}?`
          )
        ) {
          menu.hidden = true;
          clearVmContextHighlights();
          return;
        }
        if (
          action === "template" &&
          !window.confirm(`Convert ${targetRows.length} selected VM${targetRows.length === 1 ? "" : "s"} to template?`)
        ) {
          menu.hidden = true;
          clearVmContextHighlights();
          return;
        }
        submitVmBulkAction(
          activeVmOverview,
          action === "delete-snapshots" ? "delete_snapshots" : action,
          {},
          targetRows
        );
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
    initPage(currentContent);
    createIcons();
    return true;
  };

  const loadSoftNavigation = async (url, options = {}) => {
    const push = options.push !== false;

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

  const initSpaceCharts = (root) => {
    root.querySelectorAll("[data-space-chart]").forEach((svg) => {
      if (svg.dataset.chartRendered) return;
      svg.dataset.chartRendered = "1";

      let raw;
      try {
        raw = JSON.parse(svg.dataset.chartData || "[]");
      } catch (_) {
        return;
      }
      if (!raw.length) return;

      const rect = svg.getBoundingClientRect();
      const W = rect.width || 600;
      const H = 220;
      const PL = 98,
        PR = 18,
        PT = 34,
        PB = 48;
      const pW = W - PL - PR;
      const pH = H - PT - PB;
      svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

      let maxB = 0;
      raw.forEach((d) => {
        if (d.total_bytes > maxB) maxB = d.total_bytes;
      });
      if (!maxB) return;

      const ts = raw.map((d) => new Date(d.timestamp).getTime());
      const sevenDaysMs = 7 * 24 * 60 * 60 * 1000;
      const chartEnd = ts[ts.length - 1];
      const chartStart = Math.min(ts[0], chartEnd - sevenDaysMs);
      const chartRange = chartEnd - chartStart || 1;
      const xOf = (t) => PL + ((t - chartStart) / chartRange) * pW;
      const yOf = (b) => PT + pH - (b / maxB) * pH;
      const colors = {
        used: "#2f8de4",
        free: "#7c4d9e",
        total: "#35d04f",
        grid: "rgba(179, 202, 219, 0.28)",
        label: "#cfe7ff",
      };
      const fmt = (b) => {
        if (b >= 549755813888) return `${(b / 1099511627776).toFixed(1)} TB`;
        if (b >= 1073741824) return `${(b / 1073741824).toFixed(1)} GB`;
        if (b >= 1048576) return `${(b / 1048576).toFixed(1)} MB`;
        return `${(b / 1024).toFixed(1)} KB`;
      };
      const pad2 = (n) => String(n).padStart(2, "0");
      const fmtDate = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
      const fmtClock = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
      const ns = "http://www.w3.org/2000/svg";
      const el = (tag, a) => {
        const e = document.createElementNS(ns, tag);
        const styleProps = ["fill", "stroke", "opacity"];
        let styleStr = "";
        for (const k in a) {
          if (styleProps.includes(k) || k === "font-size" || k === "font-weight") {
            styleStr += `${k}:${a[k]};`;
          } else {
            e.setAttribute(k, a[k]);
          }
        }
        if (styleStr) e.setAttribute("style", styleStr);
        return e;
      };
      const tx = (tag, a, t) => {
        const e = el(tag, a);
        e.textContent = t;
        return e;
      };

      svg.appendChild(el("rect", { x: PL, y: PT, width: pW, height: pH, fill: "rgba(10, 20, 30, 0.18)" }));

      for (let i = 0; i <= 5; i++) {
        const percent = 100 - i * 20;
        const yP = PT + (pH / 5) * i;
        const v = maxB * (percent / 100);
        svg.appendChild(el("line", { x1: PL, y1: yP, x2: W - PR, y2: yP, stroke: colors.grid, "stroke-width": "1" }));
        svg.appendChild(
          tx("text", { x: PL - 42, y: yP + 4, "text-anchor": "end", fill: colors.label, "font-size": "11" }, fmt(v))
        );
        svg.appendChild(
          tx(
            "text",
            { x: PL - 6, y: yP + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": "10" },
            `${percent}%`
          )
        );
      }

      const series =
        raw.length === 1
          ? [
              { ...raw[0], chart_ts: chartStart },
              { ...raw[0], chart_ts: chartEnd },
            ]
          : raw.map((d) => ({ ...d, chart_ts: new Date(d.timestamp).getTime() }));
      const seriesTs = series.map((d) => d.chart_ts);
      const linePath = (key) => {
        let path = `M ${xOf(seriesTs[0])} ${yOf(series[0][key])}`;
        series.forEach((d, i) => {
          path += ` L ${xOf(seriesTs[i])} ${yOf(d[key])}`;
        });
        return path;
      };
      const areaToBottom = (key) => {
        let path = linePath(key);
        path += ` L ${xOf(seriesTs[seriesTs.length - 1])} ${PT + pH} L ${xOf(seriesTs[0])} ${PT + pH} Z`;
        return path;
      };
      const areaBetween = (upperKey, lowerKey) => {
        let path = linePath(upperKey);
        for (let i = series.length - 1; i >= 0; i--) {
          path += ` L ${xOf(seriesTs[i])} ${yOf(series[i][lowerKey])}`;
        }
        return `${path} Z`;
      };

      svg.appendChild(el("path", { d: areaToBottom("used_bytes"), fill: colors.used, opacity: "0.72" }));
      svg.appendChild(el("path", { d: areaBetween("total_bytes", "used_bytes"), fill: colors.free, opacity: "0.62" }));
      svg.appendChild(
        el("path", { d: linePath("used_bytes"), fill: "none", stroke: colors.used, "stroke-width": "2.5" })
      );
      svg.appendChild(
        el("path", { d: linePath("total_bytes"), fill: "none", stroke: colors.total, "stroke-width": "2.5" })
      );

      // Data points
      raw.forEach((d, i) => {
        svg.appendChild(
          el("circle", {
            cx: xOf(ts[i]),
            cy: yOf(d.used_bytes),
            r: "3.5",
            fill: colors.used,
            stroke: "var(--surface)",
            "stroke-width": "1",
          })
        );
      });

      // Time labels
      [
        [chartStart, "start"],
        [chartStart + chartRange / 2, "middle"],
        [chartEnd, "end"],
      ].forEach(([labelTs, anchor]) => {
        const dt = new Date(labelTs);
        const textAnchor = anchor === "start" ? "start" : anchor === "end" ? "end" : "middle";
        const x = anchor === "start" ? PL : anchor === "end" ? W - PR : xOf(labelTs);
        svg.appendChild(
          tx("text", { x, y: H - 20, "text-anchor": textAnchor, fill: "var(--muted)", "font-size": "10" }, fmtDate(dt))
        );
        svg.appendChild(
          tx("text", { x, y: H - 7, "text-anchor": textAnchor, fill: "var(--muted)", "font-size": "10" }, fmtClock(dt))
        );
      });

      // Legend
      const legend = [
        ["Used", colors.used, PL + 8],
        ["Free", colors.free, PL + 78],
        ["Total", colors.total, PL + 142],
      ];
      legend.forEach(([label, color, x]) => {
        svg.appendChild(el("rect", { x, y: PT - 24, width: "10", height: "10", fill: color }));
        svg.appendChild(tx("text", { x: x + 16, y: PT - 15, fill: "var(--muted)", "font-size": "11" }, label));
      });

      // Now value label
      const last = raw[raw.length - 1];
      const lastX = xOf(ts[ts.length - 1]);
      const lastLabelNearRight = lastX > W - PR - 72;
      svg.appendChild(
        tx(
          "text",
          {
            x: lastLabelNearRight ? W - PR : Math.max(PL, lastX),
            y: Math.max(PT + 11, yOf(last.used_bytes) - 8),
            "text-anchor": lastLabelNearRight ? "end" : "middle",
            fill: "#d7ecff",
            "font-size": "10",
            "font-weight": "600",
          },
          `${fmt(last.used_bytes)} used`
        )
      );
    });
  };

  const initTableFilters = (root) => {
    root.querySelectorAll("[data-table-filter]").forEach((input) => {
      if (input.dataset.filterBound) return;
      input.dataset.filterBound = "1";
      const selector = input.dataset.tableFilter || "";
      const table = selector
        ? document.querySelector(selector)
        : input.closest(".panel")?.querySelector("[data-filterable-table]");
      if (!table) return;
      input.addEventListener("input", () => {
        const q = input.value.toLowerCase().trim();
        table.querySelectorAll("tbody tr[data-filter-text]").forEach((row) => {
          row.hidden = q && !row.dataset.filterText.includes(q);
        });
        const overview = table.closest("[data-vm-overview]");
        if (overview) {
          syncVmOverviewSelection(overview);
        }
      });
    });
  };

  const initColumnPickers = (root) => {
    root.querySelectorAll("[data-column-picker]").forEach((picker) => {
      if (picker.dataset.initialized === "true") return;
      picker.dataset.initialized = "true";

      const tableName = picker.dataset.columnPicker || "";
      const table = document.querySelector(`[data-column-table="${CSS.escape(tableName)}"]`);
      if (!table) return;

      const storageKey = `pve-helper-columns-${tableName}`;
      const orderStorageKey = `${storageKey}-order`;
      const toggles = Array.from(picker.querySelectorAll("[data-column-toggle]"));
      const panel = picker.querySelector(".column-picker-panel");
      const defaultState = {};
      toggles.forEach((toggle) => {
        defaultState[toggle.dataset.columnToggle] = toggle.checked;
      });
      document.addEventListener("click", (event) => {
        if (picker.open && !picker.contains(event.target)) {
          picker.open = false;
        }
      });
      const defaultOrder = toggles.map((toggle) => toggle.dataset.columnToggle).filter(Boolean);

      let order = [...defaultOrder];
      try {
        const storedOrder = JSON.parse(localStorage.getItem(orderStorageKey) || "[]");
        if (Array.isArray(storedOrder)) {
          const known = new Set(defaultOrder);
          order = [
            ...storedOrder.filter((column) => known.has(column)),
            ...defaultOrder.filter((column) => !storedOrder.includes(column)),
          ];
        }
      } catch (_error) {
        order = [...defaultOrder];
      }

      let state = { ...defaultState };
      try {
        const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
        if (stored && typeof stored === "object") {
          state = { ...state, ...stored };
        }
      } catch (_error) {
        state = { ...defaultState };
      }

      const applyColumnOrder = () => {
        const preferredOrder = [
          ...order.filter((column) => defaultOrder.includes(column)),
          ...defaultOrder.filter((column) => !order.includes(column)),
        ];
        const normalizedOrder = preferredOrder.includes("name")
          ? ["name", ...preferredOrder.filter((column) => column !== "name")]
          : preferredOrder;
        order = normalizedOrder;
        Array.from(table.rows).forEach((row) => {
          const cells = Array.from(row.children);
          const fixedCells = cells.filter((cell) => !cell.dataset.column);
          const cellsByColumn = new Map();
          cells
            .filter((cell) => cell.dataset.column)
            .forEach((cell) => {
              cellsByColumn.set(cell.dataset.column, cell);
            });
          fixedCells.forEach((cell) => {
            row.appendChild(cell);
          });
          normalizedOrder.forEach((column) => {
            const cell = cellsByColumn.get(column);
            if (cell) {
              row.appendChild(cell);
            }
          });
        });
      };

      const saveColumnOrder = () => {
        try {
          localStorage.setItem(orderStorageKey, JSON.stringify(order));
        } catch (_error) {
          // Column order preferences are optional.
        }
      };

      const columnFromOption = (option) => option?.querySelector("[data-column-toggle]")?.dataset.columnToggle || "";

      const clearDragMarkers = () => {
        picker.querySelectorAll(".drag-over-before, .drag-over-after").forEach((option) => {
          option.classList.remove("drag-over-before", "drag-over-after");
        });
      };

      const syncPickerOrder = () => {
        if (!panel) return;
        const labelsByColumn = new Map(
          toggles.map((toggle) => [
            toggle.dataset.columnToggle,
            toggle.closest("[data-column-picker-option]") || toggle.closest("label"),
          ])
        );
        order.forEach((column) => {
          const label = labelsByColumn.get(column);
          if (label) {
            panel.appendChild(label);
          }
        });
      };

      toggles.forEach((toggle) => {
        const label = toggle.closest("label");
        if (!label || label.dataset.columnPickerOption === "true") return;
        label.dataset.columnPickerOption = "true";
        label.classList.add("column-picker-option");
        if (toggle.disabled || toggle.dataset.columnToggle === "name") return;
        const grip = document.createElement("span");
        grip.className = "column-picker-grip";
        grip.dataset.columnDragHandle = "true";
        grip.draggable = true;
        grip.title = "Drag to reorder";
        grip.setAttribute("aria-label", "Drag to reorder");
        grip.textContent = "⋮⋮";
        grip.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
        });
        label.prepend(grip);
      });

      const apply = () => {
        applyColumnOrder();
        toggles.forEach((toggle) => {
          const column = toggle.dataset.columnToggle;
          if (!column) return;
          if (!toggle.disabled) {
            toggle.checked = state[column] !== false;
          }
          const visible = toggle.disabled || toggle.checked;
          table.querySelectorAll(`[data-column="${CSS.escape(column)}"]`).forEach((cell) => {
            cell.hidden = !visible;
          });
        });
        syncPickerOrder();
      };

      toggles.forEach((toggle) => {
        toggle.addEventListener("change", () => {
          state[toggle.dataset.columnToggle] = toggle.checked;
          try {
            localStorage.setItem(storageKey, JSON.stringify(state));
          } catch (_error) {
            // Column preferences are optional.
          }
          apply();
        });
      });
      let draggedColumn = "";
      picker.addEventListener("dragstart", (event) => {
        const handle = event.target.closest("[data-column-drag-handle]");
        if (!handle || !picker.contains(handle)) return;
        const option = handle.closest("[data-column-picker-option]");
        draggedColumn = columnFromOption(option);
        if (!draggedColumn) return;
        option?.classList.add("dragging");
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", draggedColumn);
      });

      picker.addEventListener("dragover", (event) => {
        if (!draggedColumn) return;
        const option = event.target.closest("[data-column-picker-option]");
        const targetColumn = columnFromOption(option);
        if (!option || !picker.contains(option) || !targetColumn || targetColumn === draggedColumn) return;
        event.preventDefault();
        const rect = option.getBoundingClientRect();
        const after = event.clientY > rect.top + rect.height / 2;
        clearDragMarkers();
        option.classList.toggle("drag-over-before", !after);
        option.classList.toggle("drag-over-after", after);
      });

      picker.addEventListener("drop", (event) => {
        if (!draggedColumn) return;
        const option = event.target.closest("[data-column-picker-option]");
        const targetColumn = columnFromOption(option);
        if (!option || !picker.contains(option) || !targetColumn || targetColumn === draggedColumn) return;
        event.preventDefault();
        const after = option.classList.contains("drag-over-after");
        const nextOrder = order.filter((column) => column !== draggedColumn);
        const targetIndex = nextOrder.indexOf(targetColumn);
        if (targetIndex < 0) return;
        nextOrder.splice(targetIndex + (after ? 1 : 0), 0, draggedColumn);
        order = nextOrder;
        saveColumnOrder();
        apply();
      });

      picker.addEventListener("dragend", () => {
        draggedColumn = "";
        clearDragMarkers();
        picker.querySelectorAll("[data-column-picker-option].dragging").forEach((option) => {
          option.classList.remove("dragging");
        });
      });

      apply();
    });
  };

  const initSortableTables = (root) => {
    root.querySelectorAll("[data-sortable-table]").forEach((table) => {
      if (table.dataset.sortableInitialized === "true") return;
      table.dataset.sortableInitialized = "true";

      const headers = Array.from(table.querySelectorAll("thead th[data-sort]"));
      const tableName =
        table.dataset.columnTable ||
        table.id ||
        `table-${Array.from(document.querySelectorAll("[data-sortable-table]")).indexOf(table)}`;
      const storageKey = `pve-helper-sort-${tableName}`;

      const readStoredSort = () => {
        try {
          const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
          if (stored && typeof stored === "object" && stored.column && stored.direction) {
            return stored;
          }
        } catch (_error) {
          // Sorting remains usable without localStorage.
        }
        return null;
      };

      const writeStoredSort = (column, direction) => {
        try {
          localStorage.setItem(storageKey, JSON.stringify({ column, direction }));
        } catch (_error) {
          // Sorting remains usable without localStorage.
        }
      };

      const sortByHeader = (header, direction, persist = true) => {
        const index = Array.from(header.parentElement?.children || []).indexOf(header);
        if (index < 0) return;
        headers.forEach((other) => {
          other.dataset.sortDirection = "";
          other.removeAttribute("aria-sort");
        });
        header.dataset.sortDirection = direction;
        header.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");

        const numeric = header.dataset.sort === "number";
        const tbody = table.tBodies[0];
        const rows = Array.from(tbody.querySelectorAll("tr")).filter((row) => row.children.length > 1);
        rows.sort((a, b) => {
          const aCell = a.children[index];
          const bCell = b.children[index];
          const aRaw = aCell?.dataset.sortValue ?? aCell?.textContent ?? "";
          const bRaw = bCell?.dataset.sortValue ?? bCell?.textContent ?? "";
          const result = numeric
            ? Number(aRaw || 0) - Number(bRaw || 0)
            : String(aRaw).localeCompare(String(bRaw), undefined, { numeric: true, sensitivity: "base" });
          return direction === "asc" ? result : -result;
        });
        rows.forEach((row) => {
          tbody.appendChild(row);
        });
        if (persist) {
          writeStoredSort(header.dataset.column || header.textContent.trim(), direction);
        }
        const overview = table.closest("[data-vm-overview]");
        if (overview) {
          syncVmOverviewSelection(overview);
        }
      };

      table.pveHelperApplyStoredSort = () => {
        const stored = readStoredSort();
        if (!stored) return;
        const header = headers.find(
          (candidate) => (candidate.dataset.column || candidate.textContent.trim()) === stored.column
        );
        if (!header) return;
        sortByHeader(header, stored.direction === "desc" ? "desc" : "asc", false);
      };

      headers.forEach((header) => {
        header.tabIndex = 0;
        header.classList.add("sortable-heading");
        const sort = () => {
          const direction = header.dataset.sortDirection === "asc" ? "desc" : "asc";
          sortByHeader(header, direction);
        };
        header.addEventListener("click", sort);
        header.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            sort();
          }
        });
      });
      table.pveHelperApplyStoredSort();
    });
  };

  const initConfirmForms = (root) => {
    root.querySelectorAll("form[data-confirm]").forEach((form) => {
      if (form.dataset.confirmBound) return;
      form.dataset.confirmBound = "1";
      form.addEventListener("submit", (e) => {
        if (!confirm(form.dataset.confirm)) e.preventDefault();
      });
    });
  };

  const initScheduledTaskForms = (root) => {
    root.querySelectorAll("[data-scheduled-task-form]").forEach((form) => {
      if (form.dataset.initialized === "true") return;
      form.dataset.initialized = "true";

      const targetSelect = form.querySelector("[data-scheduled-target]");
      const targetNode = form.querySelector("[data-scheduled-target-node]");
      const recurrenceKind = form.querySelector("[data-recurrence-kind]");
      const recurrenceFields = Array.from(form.querySelectorAll("[data-recurrence-field]"));
      const previewExpression = form.querySelector("[data-schedule-preview-expression]");
      const previewTime = form.querySelector("[data-schedule-preview-time]");
      const previewList = form.querySelector("[data-schedule-preview-list]");
      const calendarMonth = form.querySelector("[data-schedule-calendar-month]");
      const calendarGrid = form.querySelector("[data-schedule-calendar-grid]");
      const calendarPrev = form.querySelector("[data-schedule-calendar-prev]");
      const calendarNext = form.querySelector("[data-schedule-calendar-next]");
      const monthLabels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      const ordinalNumbers = { first: 1, second: 2, third: 3, fourth: 4, fifth: 5 };
      let calendarOffset = 0;

      const enabledForRecurrence = (field, kind) => {
        const fieldKind = field.dataset.recurrenceField;
        if (fieldKind === "date") return kind === "once";
        if (fieldKind === "time") return true;
        if (fieldKind === "day") return kind === "monthly_day";
        if (fieldKind === "weekdays") return kind === "weekly" || kind === "monthly_ordinal";
        if (fieldKind === "ordinals") return kind === "monthly_ordinal";
        if (fieldKind === "months") return kind !== "once";
        if (fieldKind === "catch-up") return kind !== "once";
        return true;
      };

      const valueFor = (name) => form.querySelector(`[name="${name}"]`)?.value || "";
      const checkedValues = (name) =>
        Array.from(form.querySelectorAll(`[name="${name}"]:checked`)).map((input) => input.value);
      const pad = (value) => String(value).padStart(2, "0");
      const parsedNumber = (value, fallback, min, max) => {
        const parsed = Number.parseInt(value, 10);
        if (Number.isNaN(parsed)) return fallback;
        return Math.min(max, Math.max(min, parsed));
      };
      const runHour = () => parsedNumber(valueFor("run_hour"), 0, 0, 23);
      const runMinute = () => parsedNumber(valueFor("run_minute"), 0, 0, 59);
      const selectedMonths = () =>
        checkedValues("months")
          .map((value) => Number.parseInt(value, 10))
          .filter((value) => !Number.isNaN(value));
      const selectedWeekdays = () =>
        checkedValues("weekdays")
          .map((value) => Number.parseInt(value, 10))
          .filter((value) => !Number.isNaN(value));
      const selectedOrdinals = () => checkedValues("ordinals");
      const selectedDaysOfMonth = () =>
        valueFor("days_of_month")
          .split(",")
          .map((value) => Number.parseInt(value.trim(), 10))
          .filter((value) => !Number.isNaN(value) && value >= 1 && value <= 31);
      const localWeekday = (date) => (date.getDay() + 6) % 7;
      const formatTime = () => `${pad(runHour())}:${pad(runMinute())}`;
      const formatDateTime = (date) => {
        return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:00`;
      };
      const sameDayKey = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

      const nthWeekdayOfMonth = (year, month, weekday, ordinal) => {
        if (ordinal === "last") {
          const date = new Date(year, month + 1, 0, runHour(), runMinute(), 0, 0);
          while (localWeekday(date) !== weekday) {
            date.setDate(date.getDate() - 1);
          }
          return date;
        }
        const ordinalNumber = ordinalNumbers[ordinal];
        if (!ordinalNumber) return null;
        const date = new Date(year, month, 1, runHour(), runMinute(), 0, 0);
        const offset = (weekday - localWeekday(date) + 7) % 7;
        date.setDate(1 + offset + (ordinalNumber - 1) * 7);
        return date.getMonth() === month ? date : null;
      };

      const addOccurrence = (occurrences, seen, date, now, months) => {
        if (!date || date <= now) return;
        if (months.length && !months.includes(date.getMonth() + 1)) return;
        const key = date.getTime();
        if (seen.has(key)) return;
        seen.add(key);
        occurrences.push(date);
      };

      const computeOccurrences = (limit = 80) => {
        const kind = recurrenceKind?.value || "once";
        const now = new Date();
        const hour = runHour();
        const minute = runMinute();
        const occurrences = [];
        const seen = new Set();
        const months = kind === "once" ? [] : selectedMonths();

        if (kind === "once") {
          const dateValue = valueFor("run_date");
          if (dateValue) {
            const date = new Date(`${dateValue}T${pad(hour)}:${pad(minute)}:00`);
            addOccurrence(occurrences, seen, date, new Date(0), months);
          }
          return occurrences;
        }

        if (!months.length) return [];

        if (kind === "daily" || kind === "weekly") {
          const weekdays = kind === "weekly" ? selectedWeekdays() : [];
          const cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, minute, 0, 0);
          for (let i = 0; i < 1096 && occurrences.length < limit; i += 1) {
            if (kind === "daily" || weekdays.includes(localWeekday(cursor))) {
              addOccurrence(occurrences, seen, new Date(cursor), now, months);
            }
            cursor.setDate(cursor.getDate() + 1);
          }
        } else if (kind === "monthly_day") {
          const days = selectedDaysOfMonth();
          for (let offset = 0; offset < 84 && occurrences.length < limit; offset += 1) {
            const monthCursor = new Date(now.getFullYear(), now.getMonth() + offset, 1, hour, minute, 0, 0);
            days.forEach((day) => {
              const date = new Date(monthCursor.getFullYear(), monthCursor.getMonth(), day, hour, minute, 0, 0);
              if (date.getMonth() === monthCursor.getMonth()) {
                addOccurrence(occurrences, seen, date, now, months);
              }
            });
          }
        } else if (kind === "monthly_ordinal") {
          const weekdays = selectedWeekdays();
          const ordinals = selectedOrdinals();
          for (let offset = 0; offset < 84 && occurrences.length < limit; offset += 1) {
            const monthCursor = new Date(now.getFullYear(), now.getMonth() + offset, 1, hour, minute, 0, 0);
            ordinals.forEach((ordinal) => {
              weekdays.forEach((weekday) => {
                addOccurrence(
                  occurrences,
                  seen,
                  nthWeekdayOfMonth(monthCursor.getFullYear(), monthCursor.getMonth(), weekday, ordinal),
                  now,
                  months
                );
              });
            });
          }
        }

        return occurrences.sort((a, b) => a.getTime() - b.getTime()).slice(0, limit);
      };

      const renderCalendar = (occurrences) => {
        if (!calendarMonth || !calendarGrid) return;
        const today = new Date();
        const monthDate = new Date(today.getFullYear(), today.getMonth() + calendarOffset, 1);
        calendarMonth.textContent = `${monthLabels[monthDate.getMonth()]} ${monthDate.getFullYear()}`;
        const runDays = new Set(
          occurrences
            .filter(
              (date) => date.getFullYear() === monthDate.getFullYear() && date.getMonth() === monthDate.getMonth()
            )
            .map((date) => sameDayKey(date))
        );
        const firstOffset = localWeekday(monthDate);
        const startDate = new Date(monthDate.getFullYear(), monthDate.getMonth(), 1 - firstOffset);
        calendarGrid.innerHTML = "";
        for (let index = 0; index < 42; index += 1) {
          const date = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate() + index);
          const day = document.createElement("span");
          day.className = "scheduled-calendar-day";
          day.textContent = String(date.getDate());
          if (date.getMonth() === monthDate.getMonth()) day.classList.add("in-month");
          if (runDays.has(sameDayKey(date))) day.classList.add("has-run");
          if (sameDayKey(date) === sameDayKey(today)) day.classList.add("today");
          calendarGrid.appendChild(day);
        }
      };

      const renderPreview = () => {
        const kind = recurrenceKind?.value || "once";
        const occurrences = computeOccurrences();
        const labels = {
          once: "Once",
          daily: "Daily",
          weekly: "Weekly",
          monthly_day: "Monthly by date",
          monthly_ordinal: "Monthly by weekday",
        };
        if (previewExpression) previewExpression.textContent = labels[kind] || "Custom";
        if (previewTime) previewTime.textContent = `At ${formatTime()}`;
        if (previewList) {
          previewList.innerHTML = "";
          occurrences.slice(0, 10).forEach((date) => {
            const item = document.createElement("li");
            item.textContent = formatDateTime(date);
            previewList.appendChild(item);
          });
          if (!previewList.children.length) {
            const item = document.createElement("li");
            item.textContent = "No matching runs";
            previewList.appendChild(item);
          }
        }
        renderCalendar(occurrences);
      };

      const updateTargetNode = () => {
        if (!targetSelect || !targetNode) return;
        const selectedOption = targetSelect.selectedOptions[0];
        targetNode.value = selectedOption?.dataset.node || "-";
      };

      const update = () => {
        updateTargetNode();
        const kind = recurrenceKind?.value || "once";
        recurrenceFields.forEach((field) => {
          const enabled = enabledForRecurrence(field, kind);
          field.classList.toggle("scheduled-field-disabled", !enabled);
          field.querySelectorAll("input:not([type='hidden']), select, textarea").forEach((control) => {
            control.disabled = !enabled;
          });
        });
        renderPreview();
      };

      calendarPrev?.addEventListener("click", () => {
        calendarOffset -= 1;
        renderPreview();
      });
      calendarNext?.addEventListener("click", () => {
        calendarOffset += 1;
        renderPreview();
      });
      form.addEventListener("input", update);
      form.addEventListener("change", update);
      recurrenceKind?.addEventListener("change", update);
      update();
    });
  };

  const initGuestListFilter = (root = document) => {
    root.querySelectorAll("[data-guest-filter]").forEach((input) => {
      if (input.dataset.initialized === "true") {
        return;
      }
      input.dataset.initialized = "true";
      const pane = input.closest("[data-guest-pane]");
      const list = pane ? pane.querySelector("[data-guest-list]") : null;
      if (!list) {
        return;
      }
      const items = list.querySelectorAll("[data-filter-text]");
      const apply = () => {
        const query = input.value.trim().toLowerCase();
        items.forEach((item) => {
          const text = item.dataset.filterText || "";
          item.hidden = query !== "" && !text.includes(query);
        });
      };
      input.addEventListener("input", apply);
    });
  };

  const initSummaryCards = (root = document) => {
    const grid = root.querySelector("[data-summary-cards]");
    if (!grid || grid.dataset.initialized === "true") {
      return;
    }
    grid.dataset.initialized = "true";
    const orderKey = "pve-helper-vm-summary-order";
    const cardList = () => Array.from(grid.querySelectorAll("[data-card-key]"));

    try {
      const saved = JSON.parse(localStorage.getItem(orderKey) || "[]");
      if (Array.isArray(saved) && saved.length) {
        const byKey = new Map(cardList().map((card) => [card.dataset.cardKey, card]));
        saved.forEach((key) => {
          const card = byKey.get(key);
          if (card) {
            grid.appendChild(card);
          }
        });
      }
    } catch (_error) {
      // ignore corrupt saved order
    }

    const persist = () => {
      try {
        localStorage.setItem(orderKey, JSON.stringify(cardList().map((card) => card.dataset.cardKey)));
      } catch (_error) {
        // persistence is optional
      }
    };

    // Pointer-based drag: the grabbed card floats under the cursor, a
    // placeholder marks where it will land, and the other cards slide (FLIP).
    let dragCard = null;
    let placeholder = null;
    let startX = 0;
    let startY = 0;
    let offsetX = 0;
    let offsetY = 0;
    let active = false;

    const flip = (mutate) => {
      const others = cardList().filter((card) => card !== dragCard);
      const first = new Map(others.map((card) => [card, card.getBoundingClientRect()]));
      mutate();
      others.forEach((card) => {
        const before = first.get(card);
        if (!before) {
          return;
        }
        const after = card.getBoundingClientRect();
        const dx = before.left - after.left;
        const dy = before.top - after.top;
        if (!dx && !dy) {
          return;
        }
        card.style.transition = "none";
        card.style.transform = `translate(${dx}px, ${dy}px)`;
        requestAnimationFrame(() => {
          card.style.transition = "transform 0.16s ease";
          card.style.transform = "";
        });
      });
    };

    const beginDrag = () => {
      active = true;
      const rect = dragCard.getBoundingClientRect();
      offsetX = startX - rect.left;
      offsetY = startY - rect.top;
      placeholder = document.createElement("div");
      placeholder.className = "summary-card card-placeholder";
      placeholder.style.height = `${rect.height}px`;
      grid.insertBefore(placeholder, dragCard);
      dragCard.style.width = `${rect.width}px`;
      dragCard.style.height = `${rect.height}px`;
      dragCard.style.position = "fixed";
      dragCard.style.left = `${rect.left}px`;
      dragCard.style.top = `${rect.top}px`;
      dragCard.style.zIndex = "1000";
      dragCard.style.pointerEvents = "none";
      dragCard.classList.add("dragging");
      document.body.classList.add("cards-dragging");
    };

    const onMove = (event) => {
      if (!dragCard) {
        return;
      }
      if (!active) {
        if (Math.hypot(event.clientX - startX, event.clientY - startY) < 6) {
          return;
        }
        beginDrag();
      }
      event.preventDefault();
      dragCard.style.left = `${event.clientX - offsetX}px`;
      dragCard.style.top = `${event.clientY - offsetY}px`;
      const under = document.elementFromPoint(event.clientX, event.clientY);
      const overCard = under?.closest ? under.closest("[data-card-key]") : null;
      if (overCard && overCard !== dragCard && overCard !== placeholder && overCard.parentElement === grid) {
        const box = overCard.getBoundingClientRect();
        const before = event.clientY < box.top + box.height / 2 ? true : event.clientX < box.left + box.width / 2;
        flip(() => grid.insertBefore(placeholder, before ? overCard : overCard.nextSibling));
      }
    };

    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      if (active && dragCard) {
        dragCard.style.cssText = "";
        dragCard.classList.remove("dragging");
        if (placeholder) {
          grid.insertBefore(dragCard, placeholder);
          placeholder.remove();
        }
        persist();
      }
      document.body.classList.remove("cards-dragging");
      dragCard = null;
      placeholder = null;
      active = false;
    };

    grid.addEventListener("mousedown", (event) => {
      if (event.button !== 0) {
        return;
      }
      const card = event.target.closest("[data-card-key]");
      if (!card || card.parentElement !== grid) {
        return;
      }
      // Let clicks on interactive controls behave normally.
      if (event.target.closest("a, button, input, textarea, select, summary, details, label")) {
        return;
      }
      dragCard = card;
      startX = event.clientX;
      startY = event.clientY;
      active = false;
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  };

  const initNodeReload = (root = document) => {
    root.querySelectorAll("[data-node-reload]").forEach((select) => {
      if (select.dataset.initialized === "true") {
        return;
      }
      select.dataset.initialized = "true";
      select.addEventListener("change", () => {
        const url = new URL(window.location.href);
        url.searchParams.set("node", select.value);
        window.location.href = url.toString();
      });
    });
  };

  const initHardwareEditor = (root = document) => {
    const page = root.querySelector ? root.querySelector(".hardware-editor-page") : null;
    if (!page || page.dataset.hwInit === "true") {
      return;
    }
    page.dataset.hwInit = "true";
    const form = page.querySelector(".hardware-editor-form");

    const closeKebabs = (except) => {
      page.querySelectorAll(".hw-kebab-menu").forEach((menu) => {
        if (menu !== except) {
          menu.hidden = true;
        }
      });
    };

    const closeAddMenu = (event) => {
      const details = page.querySelector("[data-hw-add]");
      if (details && !event?.target.closest("[data-hw-add]")) {
        details.open = false;
      }
    };

    const syncHotplug = (editor) => {
      const value = editor.querySelector("[data-hotplug-value]");
      if (!value) {
        return;
      }
      value.value = Array.from(editor.querySelectorAll("[data-hotplug-token]"))
        .filter((checkbox) => checkbox.checked)
        .map((checkbox) => checkbox.dataset.hotplugToken)
        .join(",");
    };

    const syncBootOrder = (editor) => {
      const value = editor.querySelector("[data-boot-order-value]");
      if (!value) {
        return;
      }
      const enabled = Array.from(editor.querySelectorAll("[data-boot-device]"))
        .filter((row) => {
          const checkbox = row.querySelector("[data-boot-enabled]");
          return checkbox?.checked;
        })
        .map((row) => row.dataset.bootDevice)
        .filter(Boolean);
      value.value = enabled.length ? `order=${enabled.join(";")}` : "";
    };

    const resizeTextarea = (textarea) => {
      textarea.style.height = "auto";
      textarea.style.height = `${Math.max(textarea.scrollHeight, 76)}px`;
    };

    const initAutogrowTextarea = (textarea) => {
      if (textarea.dataset.autogrowInitialized === "true") {
        return;
      }
      textarea.dataset.autogrowInitialized = "true";
      resizeTextarea(textarea);
      textarea.addEventListener("input", () => resizeTextarea(textarea));
    };

    const activateDevice = (type, addBtn) => {
      if (type === "cdrom") {
        const cd = page.querySelector("#device-cdrom");
        if (cd) {
          cd.classList.add("is-open");
          const toggle = cd.querySelector("[data-hw-toggle]");
          if (toggle) {
            toggle.setAttribute("aria-expanded", "true");
          }
          cd.scrollIntoView({ behavior: "smooth", block: "center" });
        }
        return;
      }
      const template = page.querySelector(`[data-new-device="${type}"][data-new-template="true"]`);
      if (!template) {
        return;
      }
      if (
        addBtn?.hasAttribute("data-add-singleton") &&
        page.querySelector(`[data-new-device="${type}"]:not([hidden]):not([data-new-template="true"])`)
      ) {
        addBtn.disabled = true;
        return;
      }
      const item = template.cloneNode(true);
      item.dataset.newTemplate = "false";
      item.hidden = false;
      template.parentElement.insertBefore(item, template);
      item.hidden = false;
      item.classList.add("is-open");
      item.querySelectorAll("[data-new-input]").forEach((el) => {
        el.disabled = false;
      });
      item.querySelectorAll("[data-new-trigger]").forEach((el) => {
        el.checked = true;
      });
      if (addBtn?.hasAttribute("data-add-singleton")) {
        addBtn.disabled = true;
      }
      const first = item.querySelector("[data-new-required], [data-new-input]:not([hidden])");
      if (first) {
        first.focus();
      }
      item.scrollIntoView({ behavior: "smooth", block: "center" });
    };

    const deactivateNew = (item) => {
      const type = item.dataset.newDevice;
      if (item.dataset.newTemplate === "true") {
        return;
      }
      item.remove();
      const addBtn = page.querySelector(`[data-add-device="${type}"]`);
      if (
        addBtn?.hasAttribute("data-add-singleton") &&
        !page.querySelector(`[data-new-device="${type}"]:not([hidden]):not([data-new-template="true"])`)
      ) {
        addBtn.disabled = false;
      }
    };

    page.addEventListener("click", (event) => {
      const toggle = event.target.closest("[data-hw-toggle]");
      if (toggle) {
        closeAddMenu(event);
        const item = toggle.closest("[data-hw-item]");
        const open = item.classList.toggle("is-open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        return;
      }

      const kebabBtn = event.target.closest("[data-hw-kebab-toggle]");
      if (kebabBtn) {
        closeAddMenu(event);
        const menu = kebabBtn.parentElement.querySelector(".hw-kebab-menu");
        const willOpen = menu.hidden;
        closeKebabs();
        menu.hidden = !willOpen;
        event.stopPropagation();
        return;
      }

      const removeToggle = event.target.closest("[data-hw-remove-toggle]");
      if (removeToggle) {
        closeAddMenu(event);
        const item = removeToggle.closest("[data-hw-item]");
        const flag = item.querySelector(".hw-remove-flag");
        const removed = item.classList.toggle("is-removed");
        if (flag) {
          flag.checked = removed;
        }
        removeToggle.textContent = removed ? "Restore device" : "Remove device";
        closeKebabs();
        return;
      }

      const removeNew = event.target.closest("[data-hw-remove-new]");
      if (removeNew) {
        closeAddMenu(event);
        deactivateNew(removeNew.closest("[data-hw-item]"));
        closeKebabs();
        return;
      }

      const addBtn = event.target.closest("[data-add-device]");
      if (addBtn) {
        activateDevice(addBtn.dataset.addDevice, addBtn);
        closeAddMenu(event);
        return;
      }

      const bootMove = event.target.closest("[data-boot-move]");
      if (bootMove) {
        const row = bootMove.closest("[data-boot-device]");
        const editor = bootMove.closest("[data-boot-order-editor]");
        if (row && editor) {
          if (
            bootMove.dataset.bootMove === "up" &&
            row.previousElementSibling &&
            row.previousElementSibling.matches("[data-boot-device]")
          ) {
            row.parentElement.insertBefore(row, row.previousElementSibling);
          } else if (bootMove.dataset.bootMove === "down" && row.nextElementSibling) {
            row.parentElement.insertBefore(row.nextElementSibling, row);
          }
          syncBootOrder(editor);
        }
        return;
      }

      closeKebabs();
      closeAddMenu(event);
    });

    page.addEventListener("change", (event) => {
      const hotplug = event.target.closest("[data-hotplug-token]");
      if (hotplug) {
        const editor = hotplug.closest("[data-hotplug-editor]");
        if (editor) {
          syncHotplug(editor);
        }
        return;
      }

      const bootEnabled = event.target.closest("[data-boot-enabled]");
      if (bootEnabled) {
        const editor = bootEnabled.closest("[data-boot-order-editor]");
        if (editor) {
          syncBootOrder(editor);
        }
      }
    });

    let draggedBootRow = null;
    page.addEventListener("dragstart", (event) => {
      const row = event.target.closest("[data-boot-device]");
      if (!row || !event.target.closest("[data-boot-order-editor]")) {
        return;
      }
      draggedBootRow = row;
      row.classList.add("is-dragging");
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", row.dataset.bootDevice || "");
      }
    });

    page.addEventListener("dragend", () => {
      if (draggedBootRow) {
        draggedBootRow.classList.remove("is-dragging");
      }
      draggedBootRow = null;
    });

    page.addEventListener("dragover", (event) => {
      if (draggedBootRow && event.target.closest("[data-boot-order-editor]")) {
        event.preventDefault();
      }
    });

    page.addEventListener("drop", (event) => {
      if (!draggedBootRow) {
        return;
      }
      const targetRow = event.target.closest("[data-boot-device]");
      const editor = event.target.closest("[data-boot-order-editor]");
      if (!targetRow || !editor || targetRow === draggedBootRow) {
        return;
      }
      event.preventDefault();
      const box = targetRow.getBoundingClientRect();
      if (event.clientY > box.top + box.height / 2) {
        targetRow.after(draggedBootRow);
      } else {
        targetRow.before(draggedBootRow);
      }
      syncBootOrder(editor);
    });

    document.addEventListener("click", (event) => {
      if (!page.contains(event.target)) {
        closeKebabs();
        const details = page.querySelector("[data-hw-add]");
        if (details && !details.contains(event.target)) {
          details.open = false;
        }
      }
    });

    if (form) {
      form.addEventListener("submit", (event) => {
        page.querySelectorAll("[data-hw-checkbox-shadow]").forEach((shadow) => {
          shadow.remove();
        });
        page
          .querySelectorAll('[data-new-device]:not([hidden]) input[type="checkbox"][data-new-input][name]')
          .forEach((checkbox) => {
            if (!checkbox.checked) {
              const shadow = document.createElement("input");
              shadow.type = "hidden";
              shadow.name = checkbox.name;
              shadow.value = "";
              shadow.setAttribute("data-hw-checkbox-shadow", "true");
              checkbox.before(shadow);
            }
          });

        let invalid = null;
        page.querySelectorAll("[data-new-device]:not([hidden]) [data-new-required]").forEach((el) => {
          el.classList.remove("hw-invalid");
          if (!invalid && !String(el.value).trim()) {
            invalid = el;
          }
        });
        if (invalid) {
          event.preventDefault();
          const virtualTab = document.getElementById("hardware-tab-virtual");
          if (virtualTab) {
            virtualTab.checked = true;
          }
          invalid.closest("[data-hw-item]").classList.add("is-open");
          invalid.classList.add("hw-invalid");
          invalid.focus();
        }
      });
    }

    page.querySelectorAll(".hw-field textarea").forEach(initAutogrowTextarea);
    page.querySelectorAll("[data-boot-order-editor]").forEach(syncBootOrder);
  };

  const initVmRegister = (root = document) => {
    const page = root.querySelector ? root.querySelector("[data-vm-register]") : null;
    if (!page || page.dataset.initialized === "true") {
      return;
    }
    page.dataset.initialized = "true";

    // Firmware: reveal the EFI fields for OVMF and keep the summary in sync.
    const bios = page.querySelector("[data-vmreg-bios]");
    const machine = page.querySelector("select[name='machine']");
    const efi = page.querySelector("[data-vmreg-efi]");
    const summary = page.querySelector("[data-vmreg-firmware-summary]");
    const optionLabel = (select) => select?.options[select.selectedIndex]?.textContent.trim() || "";
    const syncFirmware = () => {
      if (efi) {
        efi.hidden = bios?.value !== "ovmf";
      }
      if (summary) {
        summary.textContent = `${optionLabel(bios)} · ${optionLabel(machine)}`;
      }
    };
    bios?.addEventListener("change", syncFirmware);
    machine?.addEventListener("change", syncFirmware);
    syncFirmware();

    // Network adapters: add / remove rows and keep names contiguous (nic0_*, ...).
    const nics = page.querySelector("[data-vmreg-nics]");
    const template = page.querySelector("[data-vmreg-nic-template]");
    const reindexNics = () => {
      const fields = ["model", "bridge", "vlan"];
      nics?.querySelectorAll("[data-vmreg-nic]").forEach((row, index) => {
        row.querySelectorAll("select, input").forEach((control, position) => {
          if (fields[position]) {
            control.name = `nic${index}_${fields[position]}`;
          }
        });
      });
    };

    page.addEventListener("click", (event) => {
      const addButton = event.target.closest("[data-vmreg-add-nic]");
      if (addButton && template && nics) {
        nics.appendChild(template.content.cloneNode(true));
        reindexNics();
        createIcons();
        return;
      }
      const removeButton = event.target.closest("[data-vmreg-remove-nic]");
      if (removeButton) {
        removeButton.closest("[data-vmreg-nic]")?.remove();
        reindexNics();
      }
    });
    reindexNics();
  };

  // Physical-key paste for noVNC: map pasted characters to hardware keys and
  // send them via the QEMU Extended Key Event (scancode) path, bypassing
  // QEMU's own VNC keymap so national characters land correctly on the guest.
  const CONSOLE_MODIFIER_KEYSYMS = {
    ShiftLeft: 0xffe1,
    AltRight: 0xffea,
  };

  const CONSOLE_LETTER_ROWS = "abcdefghijklmnopqrstuvwxyz"
    .split("")
    .map((letter) => [`Key${letter.toUpperCase()}`, letter, letter.toUpperCase()]);

  // German QWERTZ: y/z swapped, plus @ EUR MU on AltGr.
  const CONSOLE_LETTER_ROWS_DE = CONSOLE_LETTER_ROWS.map((row) => {
    const overrides = {
      KeyY: ["KeyY", "z", "Z"],
      KeyZ: ["KeyZ", "y", "Y"],
      KeyQ: ["KeyQ", "q", "Q", "@"],
      KeyE: ["KeyE", "e", "E", "€"],
      KeyM: ["KeyM", "m", "M", "µ"],
    };
    return overrides[row[0]] || row;
  });

  // Nordic layouts share this digit row + the key right of "0".
  const CONSOLE_NORDIC_DIGITS = [
    ["Digit1", "1", "!"], ["Digit2", "2", "\"", "@"], ["Digit3", "3", "#", "£"],
    ["Digit4", "4", "¤", "$"], ["Digit5", "5", "%", "€"], ["Digit6", "6", "&"],
    ["Digit7", "7", "/", "{"], ["Digit8", "8", "(", "["], ["Digit9", "9", ")", "]"],
    ["Digit0", "0", "=", "}"],
    ["Minus", "+", "?", "\\"],
  ];

  // Each row: [DOM code, base char, shifted char?, AltGr char?]
  const CONSOLE_KEY_ROWS = {
    "en-us": [
      ...CONSOLE_LETTER_ROWS,
      ["Digit1", "1", "!"], ["Digit2", "2", "@"], ["Digit3", "3", "#"],
      ["Digit4", "4", "$"], ["Digit5", "5", "%"], ["Digit6", "6", "^"],
      ["Digit7", "7", "&"], ["Digit8", "8", "*"], ["Digit9", "9", "("],
      ["Digit0", "0", ")"],
      ["Minus", "-", "_"], ["Equal", "=", "+"],
      ["BracketLeft", "[", "{"], ["BracketRight", "]", "}"], ["Backslash", "\\", "|"],
      ["Semicolon", ";", ":"], ["Quote", "'", "\""], ["Backquote", "`", "~"],
      ["Comma", ",", "<"], ["Period", ".", ">"], ["Slash", "/", "?"],
      ["Space", " "],
    ],
    "en-gb": [
      ...CONSOLE_LETTER_ROWS,
      ["Digit1", "1", "!"], ["Digit2", "2", "\""], ["Digit3", "3", "£"],
      ["Digit4", "4", "$", "€"], ["Digit5", "5", "%"], ["Digit6", "6", "^"],
      ["Digit7", "7", "&"], ["Digit8", "8", "*"], ["Digit9", "9", "("],
      ["Digit0", "0", ")"],
      ["Minus", "-", "_"], ["Equal", "=", "+"],
      ["BracketLeft", "[", "{"], ["BracketRight", "]", "}"], ["Backslash", "#", "~"],
      ["Semicolon", ";", ":"], ["Quote", "'", "@"], ["Backquote", "`", "¬"],
      ["Comma", ",", "<"], ["Period", ".", ">"], ["Slash", "/", "?"],
      ["IntlBackslash", "\\", "|"],
      ["Space", " "],
    ],
    de: [
      ...CONSOLE_LETTER_ROWS_DE,
      ["Digit1", "1", "!"], ["Digit2", "2", "\""], ["Digit3", "3", "§"],
      ["Digit4", "4", "$"], ["Digit5", "5", "%"], ["Digit6", "6", "&"],
      ["Digit7", "7", "/", "{"], ["Digit8", "8", "(", "["], ["Digit9", "9", ")", "]"],
      ["Digit0", "0", "=", "}"],
      ["Minus", "ß", "?", "\\"],
      ["BracketLeft", "ü", "Ü"], ["BracketRight", "+", "*", "~"],
      ["Semicolon", "ö", "Ö"], ["Quote", "ä", "Ä"], ["Backslash", "#", "'"],
      ["IntlBackslash", "<", ">", "|"],
      ["Comma", ",", ";"], ["Period", ".", ":"], ["Slash", "-", "_"],
      ["Space", " "],
    ],
    sv: [
      ...CONSOLE_LETTER_ROWS,
      ...CONSOLE_NORDIC_DIGITS,
      ["BracketLeft", "å", "Å"],
      ["Semicolon", "ö", "Ö"], ["Quote", "ä", "Ä"], ["Backslash", "'", "*"],
      ["IntlBackslash", "<", ">", "|"],
      ["Comma", ",", ";"], ["Period", ".", ":"], ["Slash", "-", "_"],
      ["Backquote", "§", "½"],
      ["Space", " "],
    ],
    no: [
      ...CONSOLE_LETTER_ROWS,
      ...CONSOLE_NORDIC_DIGITS,
      ["BracketLeft", "å", "Å"],
      ["Semicolon", "ø", "Ø"], ["Quote", "æ", "Æ"], ["Backslash", "'", "*"],
      ["IntlBackslash", "<", ">"],
      ["Comma", ",", ";"], ["Period", ".", ":"], ["Slash", "-", "_"],
      ["Backquote", "|", "§"],
      ["Space", " "],
    ],
    da: [
      ...CONSOLE_LETTER_ROWS,
      ...CONSOLE_NORDIC_DIGITS,
      ["BracketLeft", "å", "Å"],
      ["Semicolon", "æ", "Æ"], ["Quote", "ø", "Ø"], ["Backslash", "'", "*"],
      ["IntlBackslash", "<", ">", "\\"],
      ["Comma", ",", ";"], ["Period", ".", ":"], ["Slash", "-", "_"],
      ["Backquote", "½", "§"],
      ["Space", " "],
    ],
  };

  // Finnish uses the same physical layout as Swedish.
  CONSOLE_KEY_ROWS.fi = CONSOLE_KEY_ROWS.sv;

  const buildConsoleKeyIndex = (rows) => {
    const index = {};
    rows.forEach(([code, base, shift, altgr]) => {
      if (altgr) index[altgr] = { code, mods: ["AltRight"] };
      if (shift) index[shift] = { code, mods: ["ShiftLeft"] };
      if (base) index[base] = { code, mods: [] };
    });
    return index;
  };

  const CONSOLE_KEY_INDEX = Object.fromEntries(
    Object.entries(CONSOLE_KEY_ROWS).map(([id, rows]) => [id, buildConsoleKeyIndex(rows)])
  );

  const CONSOLE_CONTROL_KEYS = {
    "\n": [0xff0d, "Enter"],
    "\r": [0xff0d, "Enter"],
    "\t": [0xff09, "Tab"],
    "\b": [0xff08, "Backspace"],
    "\u001b": [0xff1b, "Escape"],
  };

  const initConsolePages = (root) => {
    root.querySelectorAll("[data-console-page]").forEach((page) => {
      if (page.dataset.initialized === "true") {
        return;
      }
      page.dataset.initialized = "true";

      const connectButton = page.querySelector("[data-console-connect]");
      const disconnectButton = page.querySelector("[data-console-disconnect]");
      const frame = page.querySelector("[data-console-frame]");
      const sideMenu = page.querySelector("[data-console-side-menu]");
      const screen = page.querySelector("[data-console-screen]");
      const status = page.querySelector("[data-console-status]");
      const keepaliveInput = page.querySelector("[data-console-keepalive-minutes]");
      const layoutSelect = page.querySelector("[data-console-keyboard-layout]");
      let rfb = null;
      let terminal = null;
      let terminalFitAddon = null;
      let terminalSocket = null;
      let resizeObserver = null;
      let connectedAtLeastOnce = false;

      const reconnectKey = `${consoleReconnectPrefix}:${page.dataset.sessionUrl || window.location.pathname}`;
      const keepaliveMinutes = () => {
        const parsed = Number.parseInt(keepaliveInput?.value || "10", 10);
        return Number.isNaN(parsed) ? 10 : Math.min(99, Math.max(1, parsed));
      };

      const saveKeepaliveMinutes = () => {
        if (!keepaliveInput) {
          return;
        }
        const minutes = keepaliveMinutes();
        keepaliveInput.value = String(minutes);
        try {
          localStorage.setItem(consoleKeepaliveKey, String(minutes));
        } catch (_error) {
          // Local storage can be unavailable in restrictive browser modes.
        }
      };

      const restoreKeepaliveMinutes = () => {
        if (!keepaliveInput) {
          return;
        }
        try {
          const stored = Number.parseInt(localStorage.getItem(consoleKeepaliveKey) || "", 10);
          if (!Number.isNaN(stored)) {
            keepaliveInput.value = String(Math.min(99, Math.max(1, stored)));
          }
        } catch (_error) {
          // Keep the template default.
        }
      };

      const currentKeyboardLayout = () => {
        const value = layoutSelect?.value || "";
        return CONSOLE_KEY_INDEX[value] ? value : "en-us";
      };

      const saveKeyboardLayout = () => {
        try {
          localStorage.setItem(consoleLayoutKey, currentKeyboardLayout());
        } catch (_error) {
          // Local storage can be unavailable in restrictive browser modes.
        }
      };

      const restoreKeyboardLayout = () => {
        if (!layoutSelect) {
          return;
        }
        try {
          const stored = localStorage.getItem(consoleLayoutKey) || "";
          if (CONSOLE_KEY_INDEX[stored]) {
            layoutSelect.value = stored;
          }
        } catch (_error) {
          // Keep the template default.
        }
      };

      const rememberReconnectWindow = () => {
        if (!connectedAtLeastOnce) {
          return;
        }
        try {
          localStorage.setItem(
            reconnectKey,
            JSON.stringify({ until: Date.now() + keepaliveMinutes() * 60 * 1000 })
          );
        } catch (_error) {
          // Local storage can be unavailable in restrictive browser modes.
        }
      };

      const clearReconnectWindow = () => {
        try {
          localStorage.removeItem(reconnectKey);
        } catch (_error) {
          // Local storage can be unavailable in restrictive browser modes.
        }
      };

      const shouldAutoReconnect = () => {
        try {
          const raw = localStorage.getItem(reconnectKey);
          if (!raw) {
            return false;
          }
          const record = JSON.parse(raw);
          if (!record || Number(record.until) <= Date.now()) {
            localStorage.removeItem(reconnectKey);
            return false;
          }
          return true;
        } catch (_error) {
          return false;
        }
      };

      const applySetting = (input) => {
        if (!rfb) {
          return;
        }
        const key = input.dataset.consoleSetting;
        if (!key) {
          return;
        }
        if (input.type === "checkbox") {
          rfb[key] = input.checked;
        } else {
          rfb[key] = Number(input.value);
        }
      };

      const applySettings = () => {
        page.querySelectorAll("[data-console-setting]").forEach(applySetting);
      };

      const loadStylesheetOnce = (url) =>
        new Promise((resolve, reject) => {
          if (!url) {
            resolve();
            return;
          }
          const existing = document.querySelector(`link[data-console-css="${CSS.escape(url)}"]`);
          if (existing) {
            resolve();
            return;
          }
          const link = document.createElement("link");
          link.rel = "stylesheet";
          link.href = url;
          link.dataset.consoleCss = url;
          link.addEventListener("load", resolve, { once: true });
          link.addEventListener("error", reject, { once: true });
          document.head.appendChild(link);
        });

      const loadScriptOnce = (url) =>
        new Promise((resolve, reject) => {
          if (!url) {
            reject(new Error("Missing console script URL."));
            return;
          }
          const existing = document.querySelector(`script[data-console-script="${CSS.escape(url)}"]`);
          if (existing) {
            if (existing.dataset.loaded === "true") {
              resolve();
            } else {
              existing.addEventListener("load", resolve, { once: true });
              existing.addEventListener("error", reject, { once: true });
            }
            return;
          }
          const script = document.createElement("script");
          script.src = url;
          script.dataset.consoleScript = url;
          script.addEventListener("load", () => {
            script.dataset.loaded = "true";
            resolve();
          }, { once: true });
          script.addEventListener("error", reject, { once: true });
          document.head.appendChild(script);
        });

      const nudgeConsoleResize = () => {
        if (!rfb) {
          if (terminalFitAddon && terminalSocket?.readyState === WebSocket.OPEN) {
            terminalFitAddon.fit();
          }
          return;
        }
        const scaleInput = page.querySelector('[data-console-setting="scaleViewport"]');
        if (scaleInput) {
          rfb.scaleViewport = scaleInput.checked;
        }
        window.dispatchEvent(new Event("resize"));
      };

      const setStatus = (message, connected = false) => {
        if (status) {
          status.textContent = message;
        }
        page.classList.toggle("console-connected", connected);
        if (connectButton) {
          connectButton.disabled = connected;
        }
        if (disconnectButton) {
          disconnectButton.disabled = !connected;
        }
      };

      const hidePanels = () => {
        page.querySelectorAll("[data-console-panel]").forEach((panel) => {
          panel.hidden = true;
        });
        page.querySelectorAll("[data-console-panel-toggle]").forEach((button) => {
          button.classList.remove("active");
        });
      };

      const disconnect = ({ remember = false } = {}) => {
        if (remember) {
          rememberReconnectWindow();
        } else {
          clearReconnectWindow();
        }
        if (rfb) {
          rfb.disconnect();
          rfb = null;
        }
        if (terminalSocket) {
          terminalSocket.close();
          terminalSocket = null;
        }
        if (terminal) {
          terminal.dispose();
          terminal = null;
          terminalFitAddon = null;
        }
        if (screen) {
          screen.innerHTML = "";
          screen.classList.remove("xterm-screen");
        }
        setStatus("Disconnected", false);
      };

      const buildConsoleWebSocketUrl = (payload) => {
        const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const wsUrl = new URL(payload.websocket_url, window.location.origin);
        wsUrl.protocol = protocol;
        return wsUrl;
      };

      const markConnected = () => {
        connectedAtLeastOnce = true;
        rememberReconnectWindow();
        setStatus("Connected", true);
        nudgeConsoleResize();
      };

      const resizeTerminal = () => {
        if (!terminalFitAddon || !terminal || terminalSocket?.readyState !== WebSocket.OPEN) {
          return;
        }
        terminalFitAddon.fit();
        terminalSocket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
      };

      const connectXterm = async (payload) => {
        await loadStylesheetOnce(page.dataset.xtermCssUrl || "");
        await loadScriptOnce(page.dataset.xtermJsUrl || "");
        await loadScriptOnce(page.dataset.xtermFitUrl || "");
        const TerminalCtor = window.Terminal?.Terminal || window.Terminal;
        const FitAddonCtor = window.FitAddon?.FitAddon || window.FitAddon;
        if (!TerminalCtor || !FitAddonCtor) {
          throw new Error("xterm.js failed to load.");
        }
        screen.innerHTML = "";
        screen.classList.add("xterm-screen");
        terminal = new TerminalCtor({
          cursorBlink: true,
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
          fontSize: 14,
          scrollback: 5000,
          convertEol: true,
          theme: {
            background: "#000000",
            foreground: "#d8d8d8",
            cursor: "#e5edf3",
            selectionBackground: "#2d5d8f",
          },
        });
        terminalFitAddon = new FitAddonCtor();
        terminal.loadAddon(terminalFitAddon);
        terminal.open(screen);
        terminal.onData((data) => {
          if (terminalSocket?.readyState === WebSocket.OPEN) {
            terminalSocket.send(JSON.stringify({ type: "data", data }));
          }
        });
        terminal.onResize((size) => {
          if (terminalSocket?.readyState === WebSocket.OPEN) {
            terminalSocket.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
          }
        });
        terminalSocket = new WebSocket(buildConsoleWebSocketUrl(payload).href);
        terminalSocket.binaryType = "arraybuffer";
        terminalSocket.addEventListener("open", () => {
          markConnected();
          window.setTimeout(() => {
            resizeTerminal();
            terminal?.focus();
          }, 50);
        });
        terminalSocket.addEventListener("message", (event) => {
          if (event.data instanceof ArrayBuffer) {
            terminal?.write(new Uint8Array(event.data));
          } else {
            terminal?.write(String(event.data || ""));
          }
        });
        terminalSocket.addEventListener("close", () => {
          terminalSocket = null;
          setStatus("Disconnected", false);
        });
        terminalSocket.addEventListener("error", () => {
          setStatus("Console disconnected", false);
        });
      };

      const connectNovnc = async (payload) => {
        const module = await import(page.dataset.novncUrl);
        const RFB = module.default;

        screen.innerHTML = "";
        screen.classList.remove("xterm-screen");
        rfb = new RFB(screen, buildConsoleWebSocketUrl(payload).href, { credentials: { password: payload.password || "" } });
        applySettings();
        rfb.focusOnClick = true;
        rfb.addEventListener("connect", markConnected);
        rfb.addEventListener("disconnect", (event) => {
          rfb = null;
          const clean = event.detail && event.detail.clean;
          setStatus(clean ? "Disconnected" : "Console disconnected", false);
        });
        rfb.addEventListener("securityfailure", (event) => {
          setStatus(event.detail?.reason || "Console security negotiation failed.", false);
        });
      };

      const readClipboardText = async () => {
        if (navigator.clipboard?.readText) {
          try {
            return await navigator.clipboard.readText();
          } catch (_error) {
            // Fall back below when the browser blocks clipboard permissions.
          }
        }
        return window.prompt("Paste text") || "";
      };

      const sendNoVncKeyStroke = (spec, keysym) => {
        const mods = spec.mods || [];
        mods.forEach((code) => rfb.sendKey(CONSOLE_MODIFIER_KEYSYMS[code], code, true));
        rfb.sendKey(keysym, spec.code, true);
        rfb.sendKey(keysym, spec.code, false);
        mods
          .slice()
          .reverse()
          .forEach((code) => rfb.sendKey(CONSOLE_MODIFIER_KEYSYMS[code], code, false));
      };

      const sendNoVncText = async (text) => {
        if (!rfb || !text) {
          return;
        }
        const index = CONSOLE_KEY_INDEX[currentKeyboardLayout()] || CONSOLE_KEY_INDEX["en-us"];
        for (const char of text) {
          const control = CONSOLE_CONTROL_KEYS[char];
          if (control) {
            rfb.sendKey(control[0], control[1]);
          } else {
            const cp = char.codePointAt(0);
            const keysym = cp > 0xff ? 0x01000000 + cp : cp;
            const spec = index[char];
            if (spec) {
              // Physical-key path: bypasses QEMU's VNC keymap entirely.
              sendNoVncKeyStroke(spec, keysym);
            } else if (keysym) {
              // Fallback for unmapped characters (e.g. dead-key glyphs).
              rfb.sendKey(keysym);
            }
          }
          await new Promise((resolve) => window.setTimeout(resolve, 5));
        }
      };

      const pasteClipboard = async () => {
        const text = await readClipboardText();
        if (!text) {
          return;
        }
        if (terminalSocket?.readyState === WebSocket.OPEN) {
          terminalSocket.send(JSON.stringify({ type: "data", data: text }));
          return;
        }
        if (rfb) {
          await sendNoVncText(text);
        }
      };

      const connect = async () => {
        if (!screen || !page.dataset.sessionUrl || !page.dataset.novncUrl) {
          return;
        }

        setStatus("Creating console session...", false);
        try {
          const response = await fetch(new URL(page.dataset.sessionUrl, window.location.origin), {
            method: "POST",
            headers: {
              Accept: "application/json",
              "X-CSRFToken": page.dataset.csrfToken || "",
            },
          });
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(payload.error || "Console session failed.");
          }

          if (payload.console_type === "xterm") {
            await connectXterm(payload);
          } else {
            await connectNovnc(payload);
          }
          setStatus("Connecting...", true);
          nudgeConsoleResize();
        } catch (error) {
          disconnect();
          setStatus(error.message || "Console connection failed.", false);
        }
      };

      const submitPowerAction = async (action) => {
        if (!page.dataset.powerUrl) {
          return;
        }
        const body = new URLSearchParams();
        body.set("action", action);
        setStatus(`Submitting ${action}...`, Boolean(rfb));
        const response = await fetch(new URL(page.dataset.powerUrl, window.location.origin), {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": page.dataset.csrfToken || "",
            "X-Requested-With": "fetch",
          },
          body,
        });
        if (!response.ok) {
          setStatus(`${action} failed`, Boolean(rfb));
          return;
        }
        setStatus(`${action} submitted`, Boolean(rfb));
        window.dispatchEvent(new Event(recentTasksRefreshEvent));
      };

      sideMenu?.querySelector("[data-console-menu-tab]")?.addEventListener("click", () => {
        sideMenu.classList.toggle("collapsed");
        hidePanels();
      });

      sideMenu?.querySelectorAll("[data-console-panel-toggle]").forEach((button) => {
        button.addEventListener("click", () => {
          const targetPanel = page.querySelector(`[data-console-panel="${CSS.escape(button.dataset.consolePanelToggle || "")}"]`);
          const shouldOpen = !targetPanel || targetPanel.hidden;
          hidePanels();
          if (targetPanel && shouldOpen) {
            targetPanel.hidden = false;
            button.classList.add("active");
          }
        });
      });

      page.querySelectorAll("[data-console-setting]").forEach((input) => {
        input.addEventListener("input", () => applySetting(input));
        input.addEventListener("change", () => applySetting(input));
      });
      restoreKeepaliveMinutes();
      keepaliveInput?.addEventListener("input", saveKeepaliveMinutes);
      keepaliveInput?.addEventListener("change", saveKeepaliveMinutes);
      restoreKeyboardLayout();
      layoutSelect?.addEventListener("change", saveKeyboardLayout);

      page.querySelectorAll("[data-console-action]").forEach((button) => {
        button.addEventListener("click", async () => {
          const action = button.dataset.consoleAction;
          if (action === "disconnect") {
            disconnect();
            return;
          }
          if (action === "reload") {
            disconnect({ remember: true });
            hidePanels();
            await connect();
            return;
          }
          if (action === "fullscreen" && frame) {
            if (document.fullscreenElement) {
              await document.exitFullscreen();
            } else {
              await frame.requestFullscreen();
            }
            return;
          }
          if (action === "ctrl-alt-del") {
            if (rfb) {
              rfb.sendCtrlAltDel();
            }
            hidePanels();
            return;
          }
          if (action === "paste-clipboard") {
            await pasteClipboard();
            hidePanels();
          }
        });
      });

      page.querySelectorAll("[data-console-power-action]").forEach((button) => {
        button.addEventListener("click", async () => {
          hidePanels();
          await submitPowerAction(button.dataset.consolePowerAction || "");
        });
      });

      const closePanelsOnOutsideClick = (event) => {
        if (sideMenu && !sideMenu.contains(event.target)) {
          hidePanels();
        }
      };
      document.addEventListener("click", closePanelsOnOutsideClick);
      registerPageCleanup(() => document.removeEventListener("click", closePanelsOnOutsideClick));

      disconnectButton?.addEventListener("click", () => disconnect());
      registerPageCleanup(() => disconnect({ remember: true }));
      if (frame && window.ResizeObserver) {
        resizeObserver = new ResizeObserver(nudgeConsoleResize);
        resizeObserver.observe(frame);
        registerPageCleanup(() => resizeObserver?.disconnect());
      }

      connectButton?.addEventListener("click", connect);
      if (shouldAutoReconnect() && connectButton && !connectButton.disabled) {
        window.setTimeout(connect, 50);
      }
    });
  };

  const initPage = (root = document) => {
    initHardwareEditor(root);
    initVmRegister(root);
    initGuestListFilter(root);
    initNodeReload(root);
    initSummaryCards(root);
    initAutoSubmitForms(root);
    initScanActions(root);
    initStorageFileManagers(root);
    initConfirmedFileActions(root);
    initConfirmForms(root);
    initScheduledTaskForms(root);
    initScheduledRuns(root);
    initSpaceCharts(root);
    initTableFilters(root);
    initColumnPickers(root);
    initSortableTables(root);
    initVmOverviewSelection(root);
    initVmOverviewAgentInfo(root);
    initVmOverviewSnapshotInfo(root);
    initVmStatusRefresh(root);
    initGuestAgentSummaries(root);
    initConsolePages(root);
    createIcons();
  };

  const initShell = () => {
    applyTheme(preferredTheme());
    applyGuestNameStyle(preferredGuestNameStyle());
    try {
      applyTaskbarState(localStorage.getItem(taskbarKey) === "true");
    } catch (_error) {
      applyTaskbarState(false);
    }

    initThemeToggle();
    initGuestNameToggle();
    initTaskbarToggle();
    initTreeModules(document);
    initContextMenu();
    initSoftNavigation();
    initPage(document);
    initRecentTasks();
  };

  initShell();
})();
