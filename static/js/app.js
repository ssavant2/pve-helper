(() => {
  const themeKey = "pve-helper-theme";
  const taskbarKey = "pve-helper-taskbar-collapsed";
  const softContentSelector = "[data-soft-nav-content]";
  const softStatusSelector = "[data-soft-nav-status]";
  const softTreeSelector = "[data-soft-nav-tree]";

  let activeLabel = "";
  let pageCleanup = [];
  let navigationController = null;

  const preferredTheme = () => {
    try {
      const storedTheme = localStorage.getItem(themeKey);
      if (storedTheme === "light" || storedTheme === "dark") {
        return storedTheme;
      }
    } catch (error) {
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

  const registerPageCleanup = (cleanup) => {
    pageCleanup.push(cleanup);
  };

  const runPageCleanup = () => {
    pageCleanup.forEach((cleanup) => cleanup());
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
      } catch (error) {
        // Theme persistence is optional; the UI still updates for this page.
      }
      applyTheme(nextTheme);
    });
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
      } catch (error) {
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
      } catch (error) {
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
        } catch (error) {
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
        } catch (error) {
          // The current button state remains usable if the status poll fails.
        }
      };

      form.addEventListener("submit", () => {
        scanWasActive = true;
        setScanButtonState(true, "Scan queued");
      });

      const intervalId = window.setInterval(() => {
        if (document.visibilityState !== "hidden") {
          loadScanStatus();
        }
      }, Number.isFinite(scanPollMs) ? scanPollMs : 5000);
      registerPageCleanup(() => window.clearInterval(intervalId));
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
    let taskPage = Number.parseInt(recentTasks.dataset.taskPage || "0", 10);
    let loadingTasks = false;

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

    const renderTaskRows = (tasks) => {
      if (!rows) {
        return;
      }

      if (!tasks.length) {
        rows.innerHTML = '<tr><td colspan="9" class="empty-state">No recent tasks.</td></tr>';
        return;
      }

      rows.innerHTML = tasks
        .map(
          (task) => `
            <tr>
              <td>${escapeHtml(task.name)}</td>
              <td>${escapeHtml(task.target)}</td>
              <td><span class="badge ${escapeHtml(task.status_class)}">${escapeHtml(task.status)}</span></td>
              <td>${escapeHtml(task.details)}</td>
              <td>${escapeHtml(task.initiator)}</td>
              <td>${escapeHtml(task.queued_for)}</td>
              <td>${escapeHtml(task.started_at)}</td>
              <td>${escapeHtml(task.finished_at)}</td>
              <td>${escapeHtml(task.server)}</td>
            </tr>
          `
        )
        .join("");
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
        pageLabel.textContent = data.total ? `${data.start_index}-${data.end_index} of ${data.total}` : "0 of 0";
      }
    };

    const loadTaskPage = async (page) => {
      if (!tasksUrl || loadingTasks) {
        return;
      }

      loadingTasks = true;
      try {
        const url = new URL(tasksUrl, window.location.origin);
        url.searchParams.set("page", String(Math.max(0, page)));
        const response = await fetch(url, {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        renderTaskRows(data.tasks || []);
        updateTaskControls(data);
      } catch (error) {
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

    window.setInterval(() => {
      if (taskPage === 0 && document.visibilityState !== "hidden") {
        loadTaskPage(0);
      }
    }, Number.isFinite(pollMs) ? pollMs : 10000);
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

  const initPage = (root = document) => {
    initAutoSubmitForms(root);
    initScanActions(root);
    initAuditLogs(root);
    createIcons();
  };

  const initShell = () => {
    applyTheme(preferredTheme());
    try {
      applyTaskbarState(localStorage.getItem(taskbarKey) === "true");
    } catch (error) {
      applyTaskbarState(false);
    }

    initThemeToggle();
    initTaskbarToggle();
    initTreeModules(document);
    initRecentTasks();
    initContextMenu();
    initSoftNavigation();
    initPage(document);
  };

  initShell();
})();
