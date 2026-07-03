(() => {
  const themeKey = "pve-helper-theme";
  const guestNameStyleKey = "pve-helper-guest-name-style";
  const taskbarKey = "pve-helper-taskbar-collapsed";
  const softContentSelector = "[data-soft-nav-content]";
  const softStatusSelector = "[data-soft-nav-status]";
  const softTreeSelector = "[data-soft-nav-tree]";
  const recentTasksRefreshEvent = "pve-helper:recent-tasks-refresh";

  let activeLabel = "";
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
        } else if (actionKind === "move") {
          if (openMovePicker(form, confirmationOptions)) {
            return;
          }
          window.alert("No folder picker is available on this page.");
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
        if (Number(task.finished_at_ms || 0) <= renderedAtMs) {
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
        if (maybeReloadCurrentStorageBrowser(loadedTasks)) {
          return;
        }
        lastLoadedTasks = loadedTasks;
        lastTaskPageData = data;
        renderTaskRows(loadedTasks);
        updateTaskControls(data);
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

  const initAuditLogs = (root = document) => {
    root.querySelectorAll("[data-audit-log]").forEach((auditLog) => {
      if (auditLog.dataset.initialized === "true") {
        return;
      }

      auditLog.dataset.initialized = "true";
      const filterButtons = auditLog.querySelectorAll("[data-audit-filter]");
      const searchInput = auditLog.querySelector("[data-audit-search]");
      const rows = auditLog.querySelectorAll("[data-audit-row]");
      const emptyRow = auditLog.querySelector("[data-audit-empty]");
      const countLabel = auditLog.querySelector("[data-audit-count]");
      let activeFilter = "all";

      const applyAuditFilters = () => {
        const query = (searchInput?.value || "").trim().toLowerCase();
        let visibleCount = 0;

        rows.forEach((row) => {
          const moduleMatches = activeFilter === "all" || row.dataset.auditModule === activeFilter;
          const searchMatches = !query || (row.dataset.auditSearch || "").includes(query);
          const visible = moduleMatches && searchMatches;
          row.hidden = !visible;
          if (visible) {
            visibleCount += 1;
          }
        });

        if (emptyRow) {
          emptyRow.hidden = visibleCount > 0;
        }
        if (countLabel) {
          countLabel.textContent = `${visibleCount} event${visibleCount === 1 ? "" : "s"}`;
        }
      };

      filterButtons.forEach((button) => {
        button.addEventListener("click", () => {
          activeFilter = button.dataset.auditFilter || "all";
          filterButtons.forEach((item) => {
            const active = item === button;
            item.classList.toggle("active", active);
            item.setAttribute("aria-pressed", active ? "true" : "false");
          });
          applyAuditFilters();
        });
      });

      if (searchInput) {
        searchInput.addEventListener("input", applyAuditFilters);
      }
    });
  };

  const initContextMenu = () => {
    const menu = document.getElementById("context-menu");
    if (!menu || menu.dataset.initialized === "true") {
      return;
    }

    menu.dataset.initialized = "true";
    document.addEventListener("contextmenu", (event) => {
      const row = event.target.closest("[data-context-label]");
      if (!row) {
        return;
      }

      event.preventDefault();
      activeLabel = row.dataset.contextLabel || "";
      menu.style.left = `${event.clientX}px`;
      menu.style.top = `${event.clientY}px`;
      menu.hidden = false;
    });

    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) {
        menu.hidden = true;
      }
    });

    menu.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) {
        return;
      }

      if (button.dataset.action === "copy-path" && activeLabel) {
        await navigator.clipboard.writeText(activeLabel);
      }

      menu.hidden = true;
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
      const table = input.closest(".panel")?.querySelector("[data-filterable-table]");
      if (!table) return;
      input.addEventListener("input", () => {
        const q = input.value.toLowerCase().trim();
        table.querySelectorAll("tbody tr[data-filter-text]").forEach((row) => {
          row.hidden = q && !row.dataset.filterText.includes(q);
        });
      });
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

  const initPage = (root = document) => {
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
    initAuditLogs(root);
    initSpaceCharts(root);
    initTableFilters(root);
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
