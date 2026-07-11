(() => {
  const themeKey = "pve-helper-theme";
  const guestNameStyleKey = "pve-helper-guest-name-style";
  const ipVersionStyleKey = "pve-helper-ip-version-style";
  const taskbarKey = "pve-helper-taskbar-collapsed";
  const sidebarCollapsedKey = "pve-helper-sidebar-collapsed";
  const sidebarWidthKey = "pve-helper-sidebar-width";
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
  let activeTaskRow = null;
  let pageCleanup = [];
  let navigationController = null;
  const activeUploads = new Map();

  const ipVersion = (value) => (String(value || "").includes(":") ? "6" : "4");

  const preferredIpVersionStyle = () => {
    try {
      return localStorage.getItem(ipVersionStyleKey) === "ipv4-only" ? "ipv4-only" : "all";
    } catch (_error) {
      return "all";
    }
  };

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

  const sidebarMaxWidth = () => Math.max(280, Math.min(720, Math.floor(window.innerWidth * 0.5)));

  const measureSidebarMinimumWidth = () => {
    const sidebar = document.querySelector("[data-sidebar]");
    const navTree = sidebar?.querySelector(".nav-tree");
    if (!sidebar || !navTree) {
      return 248;
    }

    const nodes = Array.from(navTree.children)
      .map((child) => (child.matches?.(".tree-module") ? child.querySelector(".module-node") : child))
      .filter((node) => node?.matches?.(".module-node"));
    if (!nodes.length) {
      return 248;
    }

    const probe = document.createElement("span");
    probe.className = "sidebar-measure-probe";
    probe.setAttribute("aria-hidden", "true");
    sidebar.appendChild(probe);
    const widestLabel = nodes.reduce((maxWidth, node) => {
      const label = node.querySelector(".tree-label")?.textContent?.trim() || "";
      probe.textContent = label;
      return Math.max(maxWidth, probe.getBoundingClientRect().width);
    }, 0);
    probe.remove();

    // Nav padding + caret + icon + gaps + row padding. This is intentionally
    // top-level only; deeper labels can overflow until the user expands width
    // or double-clicks the handle for full-tree auto-fit.
    return Math.ceil(Math.max(190, widestLabel + 78));
  };

  const measureSidebarExpandedWidth = () => {
    const sidebar = document.querySelector("[data-sidebar]");
    const navTree = sidebar?.querySelector(".nav-tree");
    if (!sidebar || !navTree) {
      return measureSidebarMinimumWidth();
    }

    const clone = navTree.cloneNode(true);
    clone.classList.add("sidebar-measure-tree");
    clone.querySelectorAll(".tree-module.collapsed").forEach((module) => {
      module.classList.remove("collapsed");
      module.classList.add("expanded");
      const toggle = module.querySelector("[data-tree-toggle]");
      const caret = module.querySelector("[data-tree-caret]");
      toggle?.setAttribute("aria-expanded", "true");
      if (caret) {
        caret.textContent = "v";
      }
    });
    sidebar.appendChild(clone);
    const width = Math.ceil(clone.scrollWidth + 8);
    clone.remove();
    return Math.max(measureSidebarMinimumWidth(), width);
  };

  const clampSidebarWidth = (width) => {
    const minimum = measureSidebarMinimumWidth();
    return Math.min(sidebarMaxWidth(), Math.max(minimum, width || minimum));
  };

  const storedSidebarWidth = () => {
    try {
      return Number.parseInt(localStorage.getItem(sidebarWidthKey) || "", 10);
    } catch (_error) {
      return Number.NaN;
    }
  };

  const rememberSidebarWidth = (width) => {
    try {
      localStorage.setItem(sidebarWidthKey, String(Math.round(width)));
    } catch (_error) {
      // Width persistence is a convenience, not a dependency.
    }
  };

  const applySidebarState = (collapsed) => {
    const appShell = document.querySelector(".app-shell");
    const toggle = document.querySelector("[data-sidebar-toggle]");
    if (!appShell || !toggle) {
      return;
    }

    const width = clampSidebarWidth(storedSidebarWidth());
    appShell.style.setProperty("--sidebar-width", `${width}px`);
    appShell.classList.toggle("sidebar-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    toggle.setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
  };

  const refreshSidebarWidth = () => {
    const appShell = document.querySelector(".app-shell");
    if (!appShell || appShell.classList.contains("sidebar-collapsed")) {
      return;
    }
    const width = clampSidebarWidth(storedSidebarWidth());
    appShell.style.setProperty("--sidebar-width", `${width}px`);
    rememberSidebarWidth(width);
  };

  // Clarity (vSphere) object-type icons, self-hosted as inline SVG so they sit
  // alongside Lucide. 36x36 outline shapes rendered into [data-vicon] elements.
  const CLARITY_ICONS = {
    vm: '<path d="M11,5H25V8h2V5a2,2,0,0,0-2-2H11A2,2,0,0,0,9,5v6.85h2Z"/><path d="M30,10H17v2h8v6h2V12h3V26H22V17a2,2,0,0,0-2-2H6a2,2,0,0,0-2,2V31a2,2,0,0,0,2,2H20a2,2,0,0,0,2-2V28h8a2,2,0,0,0,2-2V12A2,2,0,0,0,30,10ZM6,31V17H20v9H16V20H14v6a2,2,0,0,0,2,2h4v3Z"/>',
    container:
      '<path d="M32,30H4a2,2,0,0,1-2-2V8A2,2,0,0,1,4,6H32a2,2,0,0,1,2,2V28A2,2,0,0,1,32,30ZM4,8V28H32V8Z"/><path d="M9,25.3a.8.8,0,0,1-.8-.8v-13a.8.8,0,0,1,1.6,0v13A.8.8,0,0,1,9,25.3Z"/><path d="M14.92,25.3a.8.8,0,0,1-.8-.8v-13a.8.8,0,0,1,1.6,0v13A.8.8,0,0,1,14.92,25.3Z"/><path d="M21,25.3a.8.8,0,0,1-.8-.8v-13a.8.8,0,0,1,1.6,0v13A.8.8,0,0,1,21,25.3Z"/><path d="M27,25.3a.8.8,0,0,1-.8-.8v-13a.8.8,0,0,1,1.6,0v13A.8.8,0,0,1,27,25.3Z"/>',
    // Custom: a small VM glyph on a dog-eared page — our stand-in for vSphere's
    // proprietary "vm-template" sprite (a VM icon on a sheet of paper).
    template:
      '<path fill-rule="evenodd" d="M6,2H22L30,10V34H6V2Zm2,2V32H28V11H21V4H8Z"/><path d="M21,4l7,7h-7z"/><g transform="translate(8.4,13.4) scale(0.5)"><path d="M11,5H25V8h2V5a2,2,0,0,0-2-2H11A2,2,0,0,0,9,5v6.85h2Z"/><path d="M30,10H17v2h8v6h2V12h3V26H22V17a2,2,0,0,0-2-2H6a2,2,0,0,0-2,2V31a2,2,0,0,0,2,2H20a2,2,0,0,0,2-2V28h8a2,2,0,0,0,2-2V12A2,2,0,0,0,30,10ZM6,31V17H20v9H16V20H14v6a2,2,0,0,0,2,2h4v3Z"/></g>',
    storage:
      '<path d="M33,6.69h0c-.18-3.41-9.47-4.33-15-4.33S3,3.29,3,6.78V29.37c0,3.49,9.43,4.43,15,4.43s15-.93,15-4.43V6.78s0,0,0,0S33,6.7,33,6.69Zm-2,7.56c-.33.86-5.06,2.45-13,2.45A37.45,37.45,0,0,1,7,15.34v2.08A43.32,43.32,0,0,0,18,18.7c4,0,9.93-.48,13-2v5.17c-.33.86-5.06,2.45-13,2.45A37.45,37.45,0,0,1,7,22.92V25a43.32,43.32,0,0,0,11,1.28c4,0,9.93-.48,13-2v5.1c-.35.86-5.08,2.45-13,2.45S5.3,30.2,5,29.37V6.82C5.3,6,10,4.36,18,4.36c7.77,0,12.46,1.53,13,2.37-.52.87-5.21,2.39-13,2.39A37.6,37.6,0,0,1,7,7.76V9.85a43.53,43.53,0,0,0,11,1.27c4,0,9.93-.48,13-2Z"/>',
    host: '<path d="M26.5,2H9.5A1.5,1.5,0,0,0,8,3.5V34H28V3.5A1.5,1.5,0,0,0,26.5,2ZM26,32H10V4H26Z"/><rect x="12" y="6.2" width="12" height="1.6"/><rect x="12" y="10.2" width="12" height="1.6"/><path d="M18,22.78a3,3,0,1,0,3,3A3,3,0,0,0,18,22.78Zm0,4.5a1.5,1.5,0,1,1,1.5-1.5A1.5,1.5,0,0,1,18,27.28Z"/>',
    cluster:
      '<path d="M31.36,8H27.5v2H31V30H27.5v2H33V9.67A1.65,1.65,0,0,0,31.36,8Z"/><path d="M5,10H8.5V8H4.64A1.65,1.65,0,0,0,3,9.67V32H8.5V30H5Z"/><ellipse cx="18.01" cy="25.99" rx="1.8" ry="1.79"/><path d="M24.32,4H11.68A1.68,1.68,0,0,0,10,5.68V32H26V5.68A1.68,1.68,0,0,0,24.32,4ZM24,30H12V6H24Z"/><rect x="13.5" y="9.21" width="9" height="1.6"/>',
    nodes:
      '<path d="M10.5,34.29,2,29.39V19.58l8.5-4.9,8.5,4.9v9.81ZM4,28.23,10.5,32,17,28.23V20.74L10.5,17,4,20.74Z"/><path d="M25.5,34.29,17,29.39V19.58l8.5-4.9,8.5,4.9v9.81ZM19,28.23,25.5,32,32,28.23V20.74L25.5,17,19,20.74Z"/><path d="M18,21.32l-8.5-4.9V6.61L18,1.71l8.5,4.9v9.81Zm-6.5-6.06L18,19l6.5-3.75V7.77L18,4,11.5,7.77Z"/>',
    network:
      '<path d="M26.58,32h-18a1,1,0,1,0,0,2h18a1,1,0,0,0,0-2Z"/><path d="M17.75,2a14,14,0,0,0-14,14c0,.45,0,.89.07,1.33l0,0h0A14,14,0,1,0,17.75,2Zm0,2a12,12,0,0,1,8.44,3.48c0,.33,0,.66,0,1A18.51,18.51,0,0,0,14,8.53a2.33,2.33,0,0,0-1.14-.61l-.25,0c-.12-.42-.23-.84-.32-1.27s-.14-.81-.19-1.22A11.92,11.92,0,0,1,17.75,4Zm-3,5.87A17,17,0,0,1,25.92,10a16.9,16.9,0,0,1-3.11,7,2.28,2.28,0,0,0-2.58.57c-.35-.2-.7-.4-1-.63a16,16,0,0,1-4.93-5.23,2.25,2.25,0,0,0,.47-1.77Zm-4-3.6c0,.21.06.43.1.64.09.44.21.87.33,1.3a2.28,2.28,0,0,0-1.1,2.25A18.32,18.32,0,0,0,5.9,14.22,12,12,0,0,1,10.76,6.27Zm0,15.71A2.34,2.34,0,0,0,9.2,23.74l-.64,0A11.94,11.94,0,0,1,5.8,16.92l.11-.19a16.9,16.9,0,0,1,4.81-4.89,2.31,2.31,0,0,0,2.28.63,17.53,17.53,0,0,0,5.35,5.65c.41.27.83.52,1.25.76A2.32,2.32,0,0,0,19.78,20a16.94,16.94,0,0,1-6.2,3.11A2.34,2.34,0,0,0,10.76,22Zm7,6a11.92,11.92,0,0,1-5.81-1.51l.28-.06a2.34,2.34,0,0,0,1.57-1.79,18.43,18.43,0,0,0,7-3.5,2.29,2.29,0,0,0,3-.62,17.41,17.41,0,0,0,4.32.56l.53,0A12,12,0,0,1,17.75,28Zm6.51-8.9a2.33,2.33,0,0,0-.33-1.19,18.4,18.4,0,0,0,3.39-7.37q.75.35,1.48.78a12,12,0,0,1,.42,8.2A16,16,0,0,1,24.27,19.11Z"/>',
  };
  const renderVIcons = (root = document) => {
    const scope = root?.querySelectorAll ? root : document;
    scope.querySelectorAll("[data-vicon]").forEach((el) => {
      const name = el.getAttribute("data-vicon");
      const shape = CLARITY_ICONS[name];
      if (!shape || el.dataset.viconRendered === name) {
        return;
      }
      el.innerHTML = `<svg class="vicon" viewBox="0 0 36 36" aria-hidden="true" focusable="false">${shape}</svg>`;
      el.dataset.viconRendered = name;
    });
  };

  const createIcons = () => {
    if (window.lucide) {
      window.lucide.createIcons({
        attrs: {
          "aria-hidden": "true",
        },
      });
    }
    renderVIcons(document);
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

  // Re-order the guest list to match how it is labelled: when VMIDs are shown
  // the natural key is the numeric VMID; when they are hidden it is the name.
  const sortGuestList = (showingIds) => {
    document.querySelectorAll("[data-guest-list]").forEach((list) => {
      const items = Array.from(list.querySelectorAll("[data-guest-target]"));
      if (items.length < 2) {
        return;
      }
      items
        .sort((a, b) => {
          if (showingIds) {
            const av = parseInt(a.dataset.guestVmid || "", 10);
            const bv = parseInt(b.dataset.guestVmid || "", 10);
            if (Number.isNaN(av) && Number.isNaN(bv)) return 0;
            if (Number.isNaN(av)) return 1;
            if (Number.isNaN(bv)) return -1;
            return av - bv;
          }
          return (a.dataset.guestName || "").localeCompare(b.dataset.guestName || "", undefined, {
            sensitivity: "base",
          });
        })
        .forEach((item) => {
          list.appendChild(item);
        });
    });
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
    sortGuestList(showing);
  };

  const visibleIpText = (value) => {
    const parts = String(value || "")
      .split(/[\n,]+/)
      .map((part) => part.trim())
      .filter(Boolean);
    if (document.documentElement.dataset.ipVersionStyle === "ipv4-only") {
      return parts.filter((part) => ipVersion(part) === "4").join(", ") || "-";
    }
    return parts.join(", ") || "-";
  };

  const renderIpCell = (cell, rawValue) => {
    if (!cell) {
      return;
    }
    const raw = rawValue ?? cell.dataset.ipRaw ?? cell.textContent;
    cell.dataset.ipRaw = String(raw || "");
    const text = visibleIpText(raw);
    cell.textContent = text;
    cell.dataset.sortValue = text;
  };

  const applyIpVersionStyle = (style) => {
    const value = style === "ipv4-only" ? "ipv4-only" : "all";
    document.documentElement.dataset.ipVersionStyle = value;
    const ipv4Only = value === "ipv4-only";
    document.querySelectorAll("[data-ip-version-label]").forEach((label) => {
      label.textContent = ipv4Only ? "IPv4 only" : "IPv4+IPv6";
    });
    const toggle = document.querySelector("[data-ip-version-toggle]");
    if (toggle) {
      toggle.setAttribute("aria-pressed", ipv4Only ? "true" : "false");
      toggle.setAttribute("aria-label", ipv4Only ? "Show IPv4 and IPv6" : "Show IPv4 only");
    }
    document.querySelectorAll("[data-ip-address][data-ip-version='6']").forEach((item) => {
      item.hidden = ipv4Only;
      const nextSeparator = item.nextElementSibling;
      const previousSeparator = item.previousElementSibling;
      if (nextSeparator?.matches("[data-ip-separator]")) {
        nextSeparator.hidden = ipv4Only;
      }
      if (previousSeparator?.matches("[data-ip-separator]")) {
        previousSeparator.hidden = ipv4Only;
      }
    });
    document.querySelectorAll("[data-agent-ip-cell]").forEach((cell) => {
      renderIpCell(cell);
    });
  };

  const initIpVersionToggle = () => {
    const toggle = document.querySelector("[data-ip-version-toggle]");
    if (!toggle || toggle.dataset.initialized === "true") {
      return;
    }
    toggle.dataset.initialized = "true";
    toggle.addEventListener("click", () => {
      const next = document.documentElement.dataset.ipVersionStyle === "ipv4-only" ? "all" : "ipv4-only";
      try {
        localStorage.setItem(ipVersionStyleKey, next);
      } catch (_error) {
        // IP display persistence is optional; the UI still updates for this page.
      }
      applyIpVersionStyle(next);
    });
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

  const initSidebarControls = () => {
    const appShell = document.querySelector(".app-shell");
    const sidebar = document.querySelector("[data-sidebar]");
    const toggle = document.querySelector("[data-sidebar-toggle]");
    const handle = document.querySelector("[data-sidebar-resize-handle]");
    if (!appShell || !sidebar || !toggle || !handle || sidebar.dataset.controlsInitialized === "true") {
      return;
    }

    sidebar.dataset.controlsInitialized = "true";

    toggle.addEventListener("click", () => {
      const collapsed = !appShell.classList.contains("sidebar-collapsed");
      try {
        localStorage.setItem(sidebarCollapsedKey, collapsed ? "true" : "false");
      } catch (_error) {
        // The visual state still changes even when localStorage is unavailable.
      }
      if (!collapsed) {
        refreshSidebarWidth();
      }
      applySidebarState(collapsed);
    });

    handle.addEventListener("dblclick", () => {
      const width = clampSidebarWidth(measureSidebarExpandedWidth());
      rememberSidebarWidth(width);
      applySidebarState(false);
    });

    handle.addEventListener("pointerdown", (event) => {
      if (appShell.classList.contains("sidebar-collapsed")) {
        return;
      }
      event.preventDefault();
      appShell.classList.add("sidebar-resizing");
      handle.setPointerCapture?.(event.pointerId);

      const resize = (moveEvent) => {
        const shellLeft = appShell.getBoundingClientRect().left;
        const width = clampSidebarWidth(moveEvent.clientX - shellLeft);
        appShell.style.setProperty("--sidebar-width", `${width}px`);
        rememberSidebarWidth(width);
      };
      const stop = () => {
        appShell.classList.remove("sidebar-resizing");
        window.removeEventListener("pointermove", resize);
        window.removeEventListener("pointerup", stop);
        window.removeEventListener("pointercancel", stop);
      };

      window.addEventListener("pointermove", resize);
      window.addEventListener("pointerup", stop);
      window.addEventListener("pointercancel", stop);
    });

    window.addEventListener("resize", refreshSidebarWidth);
  };

  const initGlobalSearch = () => {
    const search = document.querySelector("[data-global-search]");
    const input = search?.querySelector("[data-global-search-input]");
    const results = search?.querySelector("[data-global-search-results]");
    const clearButton = search?.querySelector("[data-global-search-clear]");
    const searchUrl = search?.dataset.globalSearchUrl;
    if (!search || !input || !results || !searchUrl || search.dataset.initialized === "true") {
      return;
    }

    search.dataset.initialized = "true";
    let controller = null;
    let debounceTimer = null;
    let activeIndex = -1;

    const resultLinks = () => Array.from(results.querySelectorAll("[data-global-search-option]"));
    const setOpen = (open) => {
      results.hidden = !open;
      input.setAttribute("aria-expanded", open ? "true" : "false");
    };
    const setActive = (index) => {
      const links = resultLinks();
      activeIndex = links.length ? (index + links.length) % links.length : -1;
      links.forEach((link, linkIndex) => {
        link.classList.toggle("active", linkIndex === activeIndex);
      });
      if (activeIndex >= 0) {
        links[activeIndex].scrollIntoView({ block: "nearest" });
      }
    };
    const iconHtml = (result) => {
      const icon = escapeHtml(result.icon || "search");
      if (result.icon_family === "vicon") {
        return `<span class="global-search-result-icon" data-vicon="${icon}" aria-hidden="true"></span>`;
      }
      return `<span class="global-search-result-icon"><i data-lucide="${icon}" aria-hidden="true"></i></span>`;
    };
    const renderResults = (items, query) => {
      activeIndex = -1;
      if (!query) {
        results.innerHTML = "";
        setOpen(false);
        return;
      }
      if (!items.length) {
        results.innerHTML = '<div class="global-search-empty">No matching objects.</div>';
        setOpen(true);
        return;
      }

      let currentCategory = "";
      results.innerHTML = items
        .map((result) => {
          const category = result.category || "Results";
          const group =
            category !== currentCategory ? `<div class="global-search-group">${escapeHtml(category)}</div>` : "";
          currentCategory = category;
          return `${group}<a class="global-search-result" href="${escapeHtml(result.url || "#")}" data-global-search-option>
            ${iconHtml(result)}
            <span class="global-search-result-body">
              <span class="global-search-result-title">${escapeHtml(result.label || "Untitled")}</span>
              <span class="global-search-result-meta">${escapeHtml(result.meta || result.kind || "")}</span>
            </span>
          </a>`;
        })
        .join("");
      setOpen(true);
      createIcons();
    };
    const fetchResults = async () => {
      const query = input.value.trim();
      clearButton.hidden = query === "";
      if (!query) {
        controller?.abort();
        renderResults([], "");
        return;
      }
      controller?.abort();
      controller = new AbortController();
      const url = new URL(searchUrl, window.location.origin);
      url.searchParams.set("q", query);
      results.innerHTML = '<div class="global-search-empty">Searching...</div>';
      setOpen(true);
      try {
        const response = await fetch(url, {
          headers: { "X-Requested-With": "fetch" },
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(`Search failed with ${response.status}`);
        }
        const payload = await response.json();
        renderResults(payload.results || [], query);
      } catch (error) {
        if (error.name === "AbortError") {
          return;
        }
        results.innerHTML = '<div class="global-search-empty">Search failed.</div>';
        setOpen(true);
      }
    };
    const queueSearch = () => {
      window.clearTimeout(debounceTimer);
      debounceTimer = window.setTimeout(fetchResults, 160);
    };

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.addEventListener("input", queueSearch);
    input.addEventListener("focus", () => {
      if (results.innerHTML.trim()) {
        setOpen(true);
      }
    });
    input.addEventListener("keydown", (event) => {
      const links = resultLinks();
      if (event.key === "Escape") {
        setOpen(false);
        input.blur();
      } else if (event.key === "ArrowDown" && links.length) {
        event.preventDefault();
        setActive(activeIndex + 1);
      } else if (event.key === "ArrowUp" && links.length) {
        event.preventDefault();
        setActive(activeIndex - 1);
      } else if (event.key === "Enter" && activeIndex >= 0 && links[activeIndex]) {
        event.preventDefault();
        links[activeIndex].click();
      }
    });
    clearButton?.addEventListener("click", () => {
      input.value = "";
      clearButton.hidden = true;
      renderResults([], "");
      input.focus();
    });
    results.addEventListener("click", (event) => {
      if (event.target.closest("[data-global-search-option]")) {
        setOpen(false);
      }
    });
    document.addEventListener("click", (event) => {
      if (!search.contains(event.target)) {
        setOpen(false);
      }
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

  const initAuditExportDialog = (root = document) => {
    const dialog =
      root.querySelector("[data-audit-export-dialog]") || document.querySelector("[data-audit-export-dialog]");
    if (!dialog || dialog.dataset.initialized === "true") {
      return;
    }

    dialog.dataset.initialized = "true";
    const close = () => {
      dialog.querySelector("[data-audit-date-modal]")?.setAttribute("hidden", "");
      dialog.close();
    };
    document.querySelectorAll("[data-audit-export-open]").forEach((button) => {
      button.addEventListener("click", () => {
        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        }
      });
    });
    dialog.querySelector("[data-audit-export-close]")?.addEventListener("click", close);
    dialog.querySelector("[data-audit-export-cancel]")?.addEventListener("click", close);
    const padDatePart = (value) => String(value).padStart(2, "0");
    const monthNames = [
      "January",
      "February",
      "March",
      "April",
      "May",
      "June",
      "July",
      "August",
      "September",
      "October",
      "November",
      "December",
    ];
    const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const clampNumber = (value, min, max) => {
      const parsed = Number.parseInt(value, 10);
      if (Number.isNaN(parsed)) {
        return min;
      }
      return Math.min(max, Math.max(min, parsed));
    };
    const parseExportDate = (value) => {
      const match = String(value || "")
        .trim()
        .match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
      if (!match) {
        return null;
      }
      const parsed = new Date(
        Number.parseInt(match[1], 10),
        Number.parseInt(match[2], 10) - 1,
        Number.parseInt(match[3], 10),
        Number.parseInt(match[4], 10),
        Number.parseInt(match[5], 10)
      );
      return Number.isNaN(parsed.getTime()) ? null : parsed;
    };
    const formatExportDate = (date) =>
      `${date.getFullYear()}-${padDatePart(date.getMonth() + 1)}-${padDatePart(date.getDate())} ${padDatePart(
        date.getHours()
      )}:${padDatePart(date.getMinutes())}`;
    const dateModal = dialog.querySelector("[data-audit-date-modal]");
    const datePanel = dialog.querySelector("[data-audit-date-panel]");
    let activeDateGroup = null;
    let pickerDate = new Date();
    pickerDate.setSeconds(0, 0);
    let pickerYear = pickerDate.getFullYear();
    let pickerMonth = pickerDate.getMonth();

    const syncDateTimeGroup = (group) => {
      const valueInput = group.querySelector("[data-audit-export-datetime-value]");
      const displayInput = group.querySelector("[data-audit-export-display]");
      if (!valueInput || !displayInput) {
        return;
      }
      const parsed = parseExportDate(displayInput.value);
      if (!parsed) {
        valueInput.value = "";
        return;
      }
      const normalized = formatExportDate(parsed);
      displayInput.value = normalized;
      valueInput.value = normalized;
    };
    const closeDatePicker = () => {
      if (dateModal) {
        dateModal.hidden = true;
      }
    };
    const renderDatePicker = () => {
      if (!datePanel || !activeDateGroup) {
        return;
      }
      const firstOfMonth = new Date(pickerYear, pickerMonth, 1);
      const mondayOffset = (firstOfMonth.getDay() + 6) % 7;
      const daysInMonth = new Date(pickerYear, pickerMonth + 1, 0).getDate();
      const today = new Date();
      const dayCells = [];
      for (let index = 0; index < mondayOffset; index += 1) {
        dayCells.push('<span class="audit-date-picker-empty"></span>');
      }
      for (let day = 1; day <= daysInMonth; day += 1) {
        const isSelected =
          pickerDate.getFullYear() === pickerYear &&
          pickerDate.getMonth() === pickerMonth &&
          pickerDate.getDate() === day;
        const isToday =
          today.getFullYear() === pickerYear && today.getMonth() === pickerMonth && today.getDate() === day;
        dayCells.push(
          `<button type="button" class="audit-date-picker-day${isSelected ? " is-selected" : ""}${
            isToday ? " is-today" : ""
          }" data-audit-date-day="${day}">${day}</button>`
        );
      }
      datePanel.innerHTML = `
        <div class="audit-date-picker-title">
          <strong>${activeDateGroup.dataset.auditExportLabel || "Date range"}</strong>
          <button type="button" aria-label="Close date picker" data-audit-date-close>x</button>
        </div>
        <div class="audit-date-picker-header">
          <button type="button" aria-label="Previous month" data-audit-date-prev><i data-lucide="chevron-left" aria-hidden="true"></i></button>
          <strong>${monthNames[pickerMonth]} ${pickerYear}</strong>
          <button type="button" aria-label="Next month" data-audit-date-next><i data-lucide="chevron-right" aria-hidden="true"></i></button>
        </div>
        <div class="audit-date-picker-grid">
          ${weekdays.map((day) => `<span class="audit-date-picker-weekday">${day}</span>`).join("")}
          ${dayCells.join("")}
        </div>
        <div class="audit-date-picker-time">
          <label>Hour <input type="number" min="0" max="23" step="1" value="${padDatePart(pickerDate.getHours())}" data-audit-date-hour></label>
          <label>Minute <input type="number" min="0" max="59" step="1" value="${padDatePart(pickerDate.getMinutes())}" data-audit-date-minute></label>
        </div>
        <div class="audit-date-picker-actions">
          <button type="button" data-audit-date-clear>Clear</button>
          <button type="button" data-audit-date-apply>Apply</button>
        </div>
      `;
      if (window.lucide) {
        window.lucide.createIcons({ attrs: { "stroke-width": 2 } });
      }
    };

    dialog.querySelectorAll("[data-audit-export-datetime]").forEach((group) => {
      const openButton = group.querySelector("[data-audit-export-open-picker]");
      const displayInput = group.querySelector("[data-audit-export-display]");
      openButton?.addEventListener("click", () => {
        activeDateGroup = group;
        const parsed = parseExportDate(displayInput?.value);
        pickerDate = parsed || new Date();
        pickerDate.setSeconds(0, 0);
        if (!parsed) {
          pickerDate.setHours(
            clampNumber(group.dataset.auditExportDefaultHour || "00", 0, 23),
            clampNumber(group.dataset.auditExportDefaultMinute || "00", 0, 59),
            0,
            0
          );
        }
        pickerYear = pickerDate.getFullYear();
        pickerMonth = pickerDate.getMonth();
        renderDatePicker();
        if (dateModal) {
          dateModal.hidden = false;
        }
      });
    });

    dialog.querySelector(".audit-export-form")?.addEventListener("submit", () => {
      dialog.querySelectorAll("[data-audit-export-datetime]").forEach(syncDateTimeGroup);
    });
    dateModal?.addEventListener("click", (event) => {
      if (event.target === dateModal) {
        closeDatePicker();
      }
    });
    datePanel?.addEventListener("click", (event) => {
      event.stopPropagation();
      const target = event.target instanceof Element ? event.target.closest("button") : null;
      if (!target) {
        return;
      }
      if (target.matches("[data-audit-date-close]")) {
        closeDatePicker();
        return;
      }
      if (target.matches("[data-audit-date-prev]")) {
        pickerMonth -= 1;
        if (pickerMonth < 0) {
          pickerMonth = 11;
          pickerYear -= 1;
        }
        renderDatePicker();
        return;
      }
      if (target.matches("[data-audit-date-next]")) {
        pickerMonth += 1;
        if (pickerMonth > 11) {
          pickerMonth = 0;
          pickerYear += 1;
        }
        renderDatePicker();
        return;
      }
      if (target.matches("[data-audit-date-day]")) {
        pickerDate.setFullYear(pickerYear, pickerMonth, Number.parseInt(target.dataset.auditDateDay, 10));
        renderDatePicker();
        return;
      }
      if (target.matches("[data-audit-date-clear]")) {
        const valueInput = activeDateGroup?.querySelector("[data-audit-export-datetime-value]");
        const displayInput = activeDateGroup?.querySelector("[data-audit-export-display]");
        if (valueInput) {
          valueInput.value = "";
        }
        if (displayInput) {
          displayInput.value = "";
        }
        closeDatePicker();
        return;
      }
      if (target.matches("[data-audit-date-apply]")) {
        const hourInput = datePanel.querySelector("[data-audit-date-hour]");
        const minuteInput = datePanel.querySelector("[data-audit-date-minute]");
        pickerDate.setHours(clampNumber(hourInput?.value, 0, 23), clampNumber(minuteInput?.value, 0, 59), 0, 0);
        const formatted = formatExportDate(pickerDate);
        const valueInput = activeDateGroup?.querySelector("[data-audit-export-datetime-value]");
        const displayInput = activeDateGroup?.querySelector("[data-audit-export-display]");
        if (valueInput) {
          valueInput.value = formatted;
        }
        if (displayInput) {
          displayInput.value = formatted;
        }
        closeDatePicker();
      }
    });
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog) {
        close();
      }
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
            loadSoftNavigation(new URL(window.location.href), { push: false });
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

  const FILE_ACTION_META = {
    move: { action: "file.moved", name: "Move file" },
    copy: { action: "file.copied", name: "Copy file" },
    trash: { action: "file.trashed", name: "Move file to trash" },
    rename: { action: "file.renamed", name: "Rename file" },
    "new-folder": { action: "file.folder_created", name: "Create folder" },
    inflate: { action: "file.inflate_queued", name: "Inflate disk" },
  };

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
        fail((payload.errors || ["File action failed."]).join("; "));
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
        window.alert("Enter a file name for the copy.");
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
      const activeBare = String(badge.dataset.guestTarget || "").split("@")[0];
      if (!activeBare) {
        return false;
      }
      const completedMigrate = tasks.find((task) => {
        if (task.action !== "guest.migrate" || task.status_class !== "completed" || taskWasReloaded(task)) {
          return false;
        }
        const target = task.target_guest || {};
        if (`${target.type || ""}:${target.vmid || ""}` !== activeBare) {
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
        data-task-row-signature="${escapeHtml(taskRowSignature(task))}"
      >
        <td data-column="task-name" data-sort-value="${escapeHtml(task.name)}">${escapeHtml(task.name)}</td>
        <td data-column="target" data-sort-value="${escapeHtml(taskTargetSortValue(task))}">${task.target_guest ? renderGuestLabel(task.target_guest) : escapeHtml(task.target)}</td>
        <td data-column="status" data-sort-value="${escapeHtml(task.status)}"><span class="badge ${escapeHtml(task.status_class)}">${escapeHtml(task.status)}</span></td>
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

      const visibleTasks =
        taskPage === 0
          ? tasks.filter((task) => !pendingTasks.some((pendingTask) => pendingTaskMatchesLoadedTask(pendingTask, task)))
          : tasks;
      const mergedTasks = taskPage === 0 ? [...pendingTasks, ...visibleTasks] : visibleTasks;
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
        rows.innerHTML = '<tr><td colspan="9" class="empty-state">No recent tasks.</td></tr>';
        return;
      }

      renderTaskBody(mergedTasks);
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
          pendingTasks = pendingTasks.filter((pendingTask) => {
            if (pendingTask.pending_kind === "guest") {
              return !loadedTasks.some((task) => pendingTaskMatchesLoadedTask(pendingTask, task));
            }
            return !loadedTasks.some(
              (task) =>
                (task.action === "file.uploaded" || task.action === "file.folder_uploaded") &&
                task.storage_id === pendingTask.target &&
                Number(task.finished_at_ms || 0) >= Number(pendingTask.created_at_ms || 0) - 5000
            );
          });
        }
        let previousTaskStatuses = new Map();
        if (normalizedPage === 0) {
          previousTaskStatuses = taskStatusesById;
          taskStatusesById = new Map(loadedTasks.map((task) => [task.id, task.status_class]));
        }
        if (
          maybeRefreshCurrentStorageBrowser(loadedTasks) ||
          maybeRefreshCurrentGuestInventory(loadedTasks) ||
          maybeRefreshCurrentGuestDetail(loadedTasks)
        ) {
          return;
        }
        if (maybeRefreshSnapshotState(loadedTasks) || maybeRefreshBackupState(loadedTasks)) {
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

    window.pveHelperRefreshRecentTasks = () => {
      if (taskPage === 0 && document.visibilityState !== "hidden") {
        loadTaskPage(0);
      }
    };
    registerPageCleanup(() => {
      if (window.pveHelperRefreshRecentTasks) {
        delete window.pveHelperRefreshRecentTasks;
      }
    });

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
        if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
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
      });
      nodeSelect.addEventListener("change", refreshStorages);
      overwrite?.addEventListener("change", syncOverwrite);
      syncTargetNodes();
      refreshStorages();
      syncOverwrite();
    });
  };

  const submitVmBulkAction = async (overview, action, fields = {}, targetRows = null) => {
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
      const response = await fetch(form.action, {
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
            return opt.dataset.cpuReason
              ? `${opt.dataset.cpuReason}.`
              : "The target host can't run this VM's CPU model.";
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
        if (
          (optionsData?.guest_cpu || "") === "host" &&
          optionsData?.running &&
          opt?.dataset.hostCpuMatch === "false"
        ) {
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
        <p class="form-hint" data-clone-full-hint hidden></p>
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
            // Read the property, not FormData: a disabled (forced-full) checkbox
            // is omitted from FormData and would otherwise read as linked.
            clone_full: fullCheckbox?.checked ? "1" : "0",
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
    const writable = overview.dataset.vmWriteEnabled === "true";
    const allRunning = contextRows.every((item) => item.dataset.guestStatus === "running");
    const allNotRunning = contextRows.every((item) => item.dataset.guestStatus !== "running");
    const allStopped = contextRows.every((item) => item.dataset.guestStatus === "stopped");
    const allPaused = contextRows.every((item) => item.dataset.guestStatus === "paused");
    const allVms = contextRows.every((item) => item.dataset.guestType === "vm");
    const noTemplates = contextRows.every((item) => item.dataset.guestTemplate !== "true");
    const allTemplates = contextRows.every((item) => item.dataset.guestTemplate === "true");
    const allAgentEnabled = contextRows.every((item) => item.dataset.guestAgentEnabled === "true");
    const allAgentDisabled = contextRows.every((item) => item.dataset.guestAgentEnabled !== "true");
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
          <button type="button" data-vm-action="restore-backup" ${writable ? "" : "disabled"}>Restore Backup...</button>
        </div>
      </div>
      <div class="context-menu-separator"></div>
      <button type="button" data-vm-action="migrate" ${writable ? "" : "disabled"}><i data-lucide="move-right" aria-hidden="true"></i>Migrate...</button>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Template <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="clone" ${singleSelected && writable ? "" : "disabled"}>Clone...</button>
          <button type="button" data-vm-action="template" ${writable && allStopped && allVms && noTemplates ? "" : "disabled"}>Convert to Template</button>
          <button type="button" data-vm-action="untemplate" ${singleSelected && writable && allStopped && allVms && allTemplates ? "" : "disabled"}>Convert Template to VM...</button>
        </div>
      </div>
      <div class="context-menu-submenu">
        <button type="button" class="context-menu-parent">Tags <span>›</span></button>
        <div class="context-menu-submenu-panel">
          <button type="button" data-vm-action="edit-tags" ${writable ? "" : "disabled"}>Edit Tags...</button>
          <button type="button" disabled>Remove Tags...</button>
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
          if (!window.confirm("Cancel this task?")) {
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
          loadSoftNavigation(new URL("/vms/restore/", window.location.origin));
          return;
        }
        if (action === "edit-tags") {
          openTagsDialog(activeVmOverview, targetRows);
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
        if (
          action === "hibernate" &&
          !window.confirm(
            `Hibernate ${targetRows.length} selected VM${targetRows.length === 1 ? "" : "s"}? State is saved to disk and the VM stops; Power On resumes it.`
          )
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
    refreshSidebarWidth();
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
      const applyFilter = () => {
        const q = input.value.toLowerCase().trim();
        table.querySelectorAll("tbody tr[data-filter-text]").forEach((row) => {
          row.hidden = q && !row.dataset.filterText.includes(q);
        });
        const overview = table.closest("[data-vm-overview]");
        if (overview) {
          syncVmOverviewSelection(overview);
        }
      };
      input.addEventListener("input", applyFilter);
      const query = new URLSearchParams(window.location.search).get("q");
      if (query && !input.value) {
        input.value = query;
        applyFilter();
      }
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

      const applyNewOrder = (nextOrder) => {
        const known = new Set(defaultOrder);
        order = [
          ...nextOrder.filter((column) => known.has(column)),
          ...defaultOrder.filter((column) => !nextOrder.includes(column)),
        ];
        saveColumnOrder();
        apply();
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
        table.dispatchEvent(new CustomEvent("pve-helper-columns-changed"));
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

      table.addEventListener("pve-helper-column-order-changed", (event) => {
        if (!Array.isArray(event.detail?.order)) return;
        order = event.detail.order;
        syncPickerOrder();
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
        applyNewOrder(nextOrder);
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

  const initResizableColumns = (root) => {
    root.querySelectorAll("[data-resizable-columns][data-column-table]").forEach((table) => {
      if (table.dataset.resizableColumnsInitialized === "true") return;
      table.dataset.resizableColumnsInitialized = "true";

      const tableName = table.dataset.columnTable || table.id || "table";
      const allowColumnReorder = tableName !== "recent-tasks";
      const storageKey = `pve-helper-column-widths-${tableName}`;
      const orderStorageKey = `pve-helper-columns-${tableName}-order`;
      let storedWidths = {};
      try {
        const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
        if (stored && typeof stored === "object") {
          storedWidths = stored;
        }
      } catch (_error) {
        storedWidths = {};
      }

      let measureCanvas = null;
      const textWidth = (text, element) => {
        measureCanvas ||= document.createElement("canvas");
        const context = measureCanvas.getContext("2d");
        const style = window.getComputedStyle(element);
        context.font = `${style.fontStyle} ${style.fontVariant} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        return context.measureText(
          String(text || "")
            .replace(/\s+/g, " ")
            .trim()
        ).width;
      };

      const numericStyle = (element, property) => {
        const value = Number.parseFloat(window.getComputedStyle(element)[property]);
        return Number.isFinite(value) ? value : 0;
      };

      const columnCells = (column) => Array.from(table.querySelectorAll(`[data-column="${CSS.escape(column)}"]`));

      const minColumnWidth = (column) => {
        if (column === "name") return 150;
        if (column === "cpu") return 56;
        if (["cpus", "nics", "disks"].includes(column)) return 52;
        if (column === "vmid") return 64;
        if (column === "type") return 68;
        if (["status", "outcome"].includes(column)) return 82;
        if (["initiator", "user", "module"].includes(column)) return 76;
        if (["queued", "started", "finished", "time"].includes(column)) return 118;
        return 90;
      };

      const saveWidths = () => {
        try {
          localStorage.setItem(storageKey, JSON.stringify(storedWidths));
        } catch (_error) {
          // Column width preferences are optional.
        }
      };

      const allColumns = () =>
        Array.from(table.tHead?.rows?.[0]?.querySelectorAll("th[data-column]") || [])
          .map((cell) => cell.dataset.column)
          .filter(Boolean);

      let columnOrder = allColumns();
      if (allowColumnReorder) {
        try {
          const storedOrder = JSON.parse(localStorage.getItem(orderStorageKey) || "[]");
          if (Array.isArray(storedOrder)) {
            const known = new Set(columnOrder);
            columnOrder = [
              ...storedOrder.filter((column) => known.has(column)),
              ...columnOrder.filter((column) => !storedOrder.includes(column)),
            ];
          }
        } catch (_error) {
          columnOrder = allColumns();
        }
      }

      const normalizeColumnOrder = (nextOrder) => {
        const defaultOrder = allColumns();
        const known = new Set(defaultOrder);
        const normalized = [
          ...nextOrder.filter((column) => known.has(column)),
          ...defaultOrder.filter((column) => !nextOrder.includes(column)),
        ];
        if (tableName === "vm-overview" && normalized.includes("name")) {
          return ["name", ...normalized.filter((column) => column !== "name")];
        }
        return normalized;
      };

      const saveColumnOrder = () => {
        try {
          localStorage.setItem(orderStorageKey, JSON.stringify(columnOrder));
        } catch (_error) {
          // Column order preferences are optional.
        }
      };

      const applyColumnOrder = () => {
        columnOrder = normalizeColumnOrder(columnOrder);
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
          columnOrder.forEach((column) => {
            const cell = cellsByColumn.get(column);
            if (cell) {
              row.appendChild(cell);
            }
          });
        });
      };

      const applyNewColumnOrder = (nextOrder) => {
        columnOrder = normalizeColumnOrder(nextOrder);
        saveColumnOrder();
        applyColumnOrder();
        table.dispatchEvent(new CustomEvent("pve-helper-column-order-changed", { detail: { order: columnOrder } }));
        table.dispatchEvent(new CustomEvent("pve-helper-columns-changed"));
      };

      const visibleHeaderCells = () =>
        Array.from(table.tHead?.rows?.[0]?.children || []).filter((cell) => !cell.hidden);

      const defaultColumnWidths = {
        name: 260,
        state: 120,
        provisioned: 130,
        used: 110,
        cpu: 74,
        "host-mem": 110,
        "active-mem": 130,
        "guest-os": 200,
        agent: 120,
        node: 100,
        "has-snapshot": 110,
        vmid: 70,
        type: 70,
        "memory-size": 110,
        cpus: 58,
        nics: 58,
        disks: 58,
        uptime: 100,
        ip: 170,
        mac: 170,
        storage: 170,
        tags: 170,
        "task-name": 340,
        target: 260,
        status: 150,
        details: 340,
        initiator: 120,
        queued: 140,
        started: 155,
        finished: 155,
        server: 95,
        time: 160,
        module: 95,
        user: 130,
        source: 130,
        action: 220,
        object: 260,
        outcome: 105,
      };

      const baseCellWidth = (cell) => {
        if (cell.classList.contains("vm-select-column")) return 36;
        const column = cell.dataset.column || "";
        return Number(storedWidths[column]) || defaultColumnWidths[column] || minColumnWidth(column);
      };

      const updateTableWidth = () => {
        const headerCells = visibleHeaderCells();
        const total = headerCells.reduce((sum, cell) => sum + baseCellWidth(cell), 0);
        const scroll = table.closest(".data-table-scroll, .task-table-wrap") || table.parentElement;
        const available = scroll?.clientWidth || table.parentElement?.clientWidth || 0;
        const width = Math.max(Math.ceil(available), 1);
        const stretchCell = headerCells.filter((cell) => cell.dataset.column).at(-1);
        const stretchColumn = stretchCell?.dataset.column || "";
        const slack = stretchColumn ? width - total : 0;
        headerCells.forEach((cell) => {
          const column = cell.dataset.column || "";
          const renderedWidth = Math.max(
            column && column === stretchColumn ? minColumnWidth(column) : baseCellWidth(cell),
            baseCellWidth(cell) + (column && column === stretchColumn ? slack : 0)
          );
          const targets = column ? columnCells(column) : [cell];
          targets.forEach((target) => {
            target.style.width = `${renderedWidth}px`;
            target.style.minWidth = "0";
          });
        });
        table.style.width = `${width}px`;
        table.style.minWidth = `${width}px`;
      };

      const setColumnWidth = (column, width, persist = true) => {
        const normalized = Math.max(minColumnWidth(column), Math.round(width));
        columnCells(column).forEach((cell) => {
          cell.style.width = `${normalized}px`;
          cell.style.minWidth = `${normalized}px`;
        });
        storedWidths[column] = normalized;
        if (persist) {
          saveWidths();
        }
        updateTableWidth();
      };

      const autoFitWidth = (header) => {
        const column = header.dataset.column;
        if (!column) return header.getBoundingClientRect().width;
        const headerPadding = numericStyle(header, "paddingLeft") + numericStyle(header, "paddingRight");
        const headerControlAllowance = 10;
        let width =
          textWidth(header.dataset.columnLabel || header.textContent, header) + headerPadding + headerControlAllowance;
        table.querySelectorAll(`tbody tr:not([hidden]) [data-column="${CSS.escape(column)}"]`).forEach((cell) => {
          if (cell.hidden) return;
          const padding = numericStyle(cell, "paddingLeft") + numericStyle(cell, "paddingRight");
          const iconAllowance = column === "name" ? 28 : 0;
          width = Math.max(width, textWidth(cell.textContent, cell) + padding + iconAllowance + 4);
        });
        return Math.ceil(width);
      };

      table.querySelectorAll("thead th[data-column]").forEach((header) => {
        const column = header.dataset.column;
        if (!column) return;
        header.dataset.columnLabel ||= header.textContent.trim();
        if (allowColumnReorder && !(tableName === "vm-overview" && column === "name")) {
          header.draggable = true;
          header.title =
            header.title ||
            (table.matches("[data-sortable-table]") ? "Drag to reorder. Click to sort." : "Drag to reorder.");
        }
        const storedWidth = Number(storedWidths[column]);
        if (Number.isFinite(storedWidth) && storedWidth > 0) {
          setColumnWidth(column, storedWidth, false);
        }
        if (header.querySelector("[data-column-resize-handle]")) return;
        const handle = document.createElement("span");
        handle.className = "column-resize-handle";
        handle.dataset.columnResizeHandle = "true";
        handle.title = "Drag to resize. Double-click to fit.";
        handle.setAttribute("aria-hidden", "true");
        header.appendChild(handle);

        handle.addEventListener("click", (event) => {
          event.preventDefault();
          event.stopPropagation();
        });

        handle.addEventListener("dblclick", (event) => {
          event.preventDefault();
          event.stopPropagation();
          setColumnWidth(column, autoFitWidth(header));
        });

        handle.addEventListener("pointerdown", (event) => {
          if (event.button !== 0) return;
          event.preventDefault();
          event.stopPropagation();
          const startX = event.clientX;
          const startWidth = header.getBoundingClientRect().width;
          table.classList.add("column-resizing");
          handle.classList.add("active");

          const onPointerMove = (moveEvent) => {
            moveEvent.preventDefault();
            setColumnWidth(column, startWidth + moveEvent.clientX - startX, false);
          };

          const onPointerUp = () => {
            table.classList.remove("column-resizing");
            handle.classList.remove("active");
            saveWidths();
            document.removeEventListener("pointermove", onPointerMove);
            document.removeEventListener("pointerup", onPointerUp);
            document.removeEventListener("pointercancel", onPointerUp);
          };

          document.addEventListener("pointermove", onPointerMove);
          document.addEventListener("pointerup", onPointerUp);
          document.addEventListener("pointercancel", onPointerUp);
        });
      });

      let draggedHeaderColumn = "";
      table.addEventListener("dragstart", (event) => {
        if (!allowColumnReorder) return;
        const header = event.target.closest("thead th[data-column]");
        const column = header?.dataset.column || "";
        if (!header || !table.contains(header) || !column || (tableName === "vm-overview" && column === "name")) return;
        draggedHeaderColumn = column;
        header.classList.add("column-dragging");
        table.dataset.columnDragging = "true";
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", column);
      });

      table.addEventListener("dragover", (event) => {
        if (!allowColumnReorder) return;
        if (!draggedHeaderColumn) return;
        const header = event.target.closest("thead th[data-column]");
        const targetColumn = header?.dataset.column || "";
        if (
          !header ||
          !table.contains(header) ||
          header.hidden ||
          !targetColumn ||
          targetColumn === draggedHeaderColumn
        )
          return;
        if (tableName === "vm-overview" && targetColumn === "name") return;
        event.preventDefault();
        const rect = header.getBoundingClientRect();
        const after = event.clientX > rect.left + rect.width / 2;
        table.querySelectorAll("thead th.drag-over-before, thead th.drag-over-after").forEach((cell) => {
          cell.classList.remove("drag-over-before", "drag-over-after");
        });
        header.classList.toggle("drag-over-before", !after);
        header.classList.toggle("drag-over-after", after);
      });

      table.addEventListener("drop", (event) => {
        if (!allowColumnReorder) return;
        if (!draggedHeaderColumn) return;
        const header = event.target.closest("thead th[data-column]");
        const targetColumn = header?.dataset.column || "";
        if (
          !header ||
          !table.contains(header) ||
          header.hidden ||
          !targetColumn ||
          targetColumn === draggedHeaderColumn
        )
          return;
        if (tableName === "vm-overview" && targetColumn === "name") return;
        event.preventDefault();
        const after = header.classList.contains("drag-over-after");
        const visibleColumns = new Set(
          Array.from(table.querySelectorAll("thead th[data-column]:not([hidden])"))
            .map((cell) => cell.dataset.column)
            .filter(Boolean)
        );
        const visibleOrder = columnOrder.filter((column) => visibleColumns.has(column));
        const hiddenOrder = columnOrder.filter((column) => !visibleColumns.has(column));
        const nextVisibleOrder = visibleOrder.filter((column) => column !== draggedHeaderColumn);
        const targetIndex = nextVisibleOrder.indexOf(targetColumn);
        if (targetIndex < 0) return;
        nextVisibleOrder.splice(targetIndex + (after ? 1 : 0), 0, draggedHeaderColumn);
        const nextOrder = [...nextVisibleOrder, ...hiddenOrder.filter((column) => column !== draggedHeaderColumn)];
        table.dataset.suppressSortClick = "true";
        applyNewColumnOrder(nextOrder);
      });

      table.addEventListener("dragend", () => {
        if (!allowColumnReorder) return;
        draggedHeaderColumn = "";
        delete table.dataset.columnDragging;
        table
          .querySelectorAll("thead th.column-dragging, thead th.drag-over-before, thead th.drag-over-after")
          .forEach((cell) => {
            cell.classList.remove("column-dragging", "drag-over-before", "drag-over-after");
          });
        window.setTimeout(() => {
          delete table.dataset.suppressSortClick;
        }, 200);
      });

      table.addEventListener("pve-helper-columns-changed", updateTableWidth);
      window.addEventListener("resize", updateTableWidth);
      registerPageCleanup(() => window.removeEventListener("resize", updateTableWidth));
      applyColumnOrder();
      updateTableWidth();
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
          if (table.dataset.columnDragging === "true" || table.dataset.suppressSortClick === "true") {
            delete table.dataset.suppressSortClick;
            return;
          }
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
      const apply = () => {
        const query = input.value.trim().toLowerCase();
        const items = Array.from(list.querySelectorAll("[data-filter-text]"));
        let visibleCount = 0;
        items.forEach((item) => {
          const text = item.dataset.filterText || "";
          const hidden = query !== "" && !text.includes(query);
          item.hidden = hidden;
          if (!hidden) {
            visibleCount += 1;
          }
        });
        const empty = list.querySelector("[data-guest-filter-empty]");
        if (empty) {
          empty.hidden = query === "" || visibleCount > 0;
        }
      };
      input.addEventListener("input", apply);
      apply();
    });
  };

  const initSummaryCards = (root = document) => {
    const grid = root.querySelector("[data-summary-cards]");
    if (!grid || grid.dataset.initialized === "true") {
      return;
    }
    grid.dataset.initialized = "true";
    const orderKey = "pve-helper-vm-summary-order";
    const sizeKey = "pve-helper-vm-summary-card-sizes";
    const layoutKey = "pve-helper-vm-summary-layout-v2";
    const cardList = () => Array.from(grid.querySelectorAll("[data-card-key]"));
    const autoCardList = () => cardList().filter((card) => card.dataset.cardSize === "auto");

    const savedSizes = () => {
      try {
        const saved = JSON.parse(localStorage.getItem(sizeKey) || "{}");
        return saved && typeof saved === "object" ? saved : {};
      } catch (_error) {
        return {};
      }
    };

    const persistSizes = () => {
      const sizes = {};
      autoCardList().forEach((card) => {
        if (card.dataset.cardExpanded === "true") {
          sizes[card.dataset.cardKey] = "expanded";
        }
      });
      try {
        localStorage.setItem(sizeKey, JSON.stringify(sizes));
      } catch (_error) {
        // persistence is optional
      }
    };

    const syncSizeToggle = (card) => {
      const button = card.querySelector("[data-card-size-toggle]");
      if (!button) {
        return;
      }
      const expanded = card.dataset.cardExpanded === "true";
      const label = card.querySelector(".panel-heading h2")?.textContent?.trim() || "card";
      button.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${label}`);
      button.title = expanded ? "Collapse" : "Expand";
    };

    const cardSpan = (card) => (card.dataset.cardSize === "full" || card.dataset.cardExpanded === "true" ? 2 : 1);

    const gridMetrics = () => {
      const style = window.getComputedStyle(grid);
      const columns = style.gridTemplateColumns.split(" ").filter((value) => value && value !== "none").length || 1;
      const columnGap = Number.parseFloat(style.columnGap) || 0;
      const rowGap = Number.parseFloat(style.rowGap) || 0;
      const rect = grid.getBoundingClientRect();
      const columnWidth = (rect.width - columnGap * Math.max(0, columns - 1)) / columns;
      const rowHeight = Number.parseFloat(style.gridAutoRows) || columnWidth;
      return { columnGap, columns, columnWidth, rect, rowGap, rowHeight };
    };

    const cardGridPosition = (card, metrics) => {
      const rect = card.getBoundingClientRect();
      const rowStride = metrics.rowHeight + metrics.rowGap;
      const columnStride = metrics.columnWidth + metrics.columnGap;
      return {
        x: Math.max(
          0,
          Math.min(metrics.columns - 1, Math.round((rect.left - metrics.rect.left) / Math.max(1, columnStride)))
        ),
        y: Math.max(0, Math.round((rect.top - metrics.rect.top) / Math.max(1, rowStride))),
      };
    };

    const pointerGridPosition = (event, metrics) => {
      const columnStride = metrics.columnWidth + metrics.columnGap;
      const rowStride = metrics.rowHeight + metrics.rowGap;
      if (event.clientX < metrics.rect.left || event.clientX > metrics.rect.right || event.clientY < metrics.rect.top) {
        return null;
      }
      return {
        x: Math.max(
          0,
          Math.min(metrics.columns - 1, Math.floor((event.clientX - metrics.rect.left) / Math.max(1, columnStride)))
        ),
        y: Math.max(0, Math.floor((event.clientY - metrics.rect.top) / Math.max(1, rowStride))),
      };
    };

    const loadLayout = () => {
      try {
        const saved = JSON.parse(localStorage.getItem(layoutKey) || "{}");
        return saved && typeof saved === "object" ? saved : {};
      } catch (_error) {
        return {};
      }
    };

    const persistLayout = (layout) => {
      try {
        localStorage.setItem(layoutKey, JSON.stringify(layout));
      } catch (_error) {
        // persistence is optional
      }
    };

    const layoutFromDom = () => {
      const metrics = gridMetrics();
      const layout = {};
      cardList().forEach((card) => {
        layout[card.dataset.cardKey] = cardGridPosition(card, metrics);
      });
      return layout;
    };

    const hasCollision = (item, placed) =>
      placed.find((other) => item.x === other.x && item.y < other.y + other.span && item.y + item.span > other.y);

    const resolveLayout = (layout, preferredKey = "") => {
      const metrics = gridMetrics();
      const byKey = new Map(cardList().map((card) => [card.dataset.cardKey, card]));
      const items = cardList().map((card) => {
        const saved = layout[card.dataset.cardKey] || {};
        return {
          key: card.dataset.cardKey,
          x: Math.max(0, Math.min(metrics.columns - 1, Number.isFinite(saved.x) ? saved.x : 0)),
          y: Math.max(0, Number.isFinite(saved.y) ? saved.y : 0),
          span: cardSpan(card),
        };
      });
      items.sort((left, right) => {
        if (left.key === preferredKey) {
          return -1;
        }
        if (right.key === preferredKey) {
          return 1;
        }
        return (
          left.y - right.y ||
          left.x - right.x ||
          cardList().indexOf(byKey.get(left.key)) - cardList().indexOf(byKey.get(right.key))
        );
      });
      const placed = [];
      items.forEach((item) => {
        let collision = hasCollision(item, placed);
        while (collision) {
          item.y = collision.y + collision.span;
          collision = hasCollision(item, placed);
        }
        placed.push(item);
      });
      return Object.fromEntries(placed.map((item) => [item.key, { x: item.x, y: item.y }]));
    };

    const applyLayout = (layout) => {
      cardList().forEach((card) => {
        const position = layout[card.dataset.cardKey] || { x: 0, y: 0 };
        card.style.gridColumn = `${position.x + 1} / span 1`;
        card.style.gridRow = `${position.y + 1} / span ${cardSpan(card)}`;
      });
    };

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
    const sizes = savedSizes();
    autoCardList().forEach((card) => {
      if (sizes[card.dataset.cardKey] === "expanded") {
        card.dataset.cardExpanded = "true";
      }
      syncSizeToggle(card);
    });

    let activeLayout = resolveLayout(Object.keys(loadLayout()).length ? loadLayout() : layoutFromDom());
    applyLayout(activeLayout);

    grid.querySelectorAll("[data-card-size-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const card = button.closest("[data-card-key]");
        if (!card) {
          return;
        }
        if (card.dataset.cardExpanded === "true") {
          delete card.dataset.cardExpanded;
        } else {
          card.dataset.cardExpanded = "true";
        }
        syncSizeToggle(card);
        persistSizes();
        activeLayout = resolveLayout(activeLayout, card.dataset.cardKey);
        applyLayout(activeLayout);
        persistLayout(activeLayout);
      });
    });

    const persist = () => {
      try {
        localStorage.setItem(orderKey, JSON.stringify(cardList().map((card) => card.dataset.cardKey)));
      } catch (_error) {
        // persistence is optional
      }
    };

    // Pointer-based drag with explicit grid slots. The layout is sparse on
    // purpose: empty half-slots can remain between cards, just like a dashboard
    // grid with manual placement.
    let dragCard = null;
    let placeholder = null;
    let previewLayout = null;
    let startX = 0;
    let startY = 0;
    let offsetX = 0;
    let offsetY = 0;
    let active = false;

    const beginDrag = () => {
      active = true;
      const rect = dragCard.getBoundingClientRect();
      activeLayout = resolveLayout(activeLayout);
      applyLayout(activeLayout);
      offsetX = startX - rect.left;
      offsetY = startY - rect.top;
      placeholder = document.createElement("div");
      placeholder.className = "summary-card card-placeholder";
      placeholder.dataset.cardSize = dragCard.dataset.cardSize || "full";
      if (dragCard.dataset.cardExpanded === "true") {
        placeholder.dataset.cardExpanded = "true";
      }
      const startPosition = activeLayout[dragCard.dataset.cardKey] || cardGridPosition(dragCard, gridMetrics());
      placeholder.style.gridColumn = `${startPosition.x + 1} / span 1`;
      placeholder.style.gridRow = `${startPosition.y + 1} / span ${cardSpan(dragCard)}`;
      grid.appendChild(placeholder);
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
      const target = pointerGridPosition(event, gridMetrics());
      if (!target) {
        return;
      }
      previewLayout = resolveLayout(
        {
          ...activeLayout,
          [dragCard.dataset.cardKey]: target,
        },
        dragCard.dataset.cardKey
      );
      const previewPosition = previewLayout[dragCard.dataset.cardKey] || target;
      placeholder.style.gridColumn = `${previewPosition.x + 1} / span 1`;
      placeholder.style.gridRow = `${previewPosition.y + 1} / span ${cardSpan(dragCard)}`;
      applyLayout(previewLayout);
    };

    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      if (active && dragCard) {
        activeLayout = previewLayout || activeLayout;
        dragCard.style.cssText = "";
        dragCard.classList.remove("dragging");
        placeholder?.remove();
        applyLayout(activeLayout);
        persistLayout(activeLayout);
        persist();
      }
      document.body.classList.remove("cards-dragging");
      dragCard = null;
      placeholder = null;
      previewLayout = null;
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
        loadSoftNavigation(url);
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
    ["Digit1", "1", "!"],
    ["Digit2", "2", '"', "@"],
    ["Digit3", "3", "#", "£"],
    ["Digit4", "4", "¤", "$"],
    ["Digit5", "5", "%", "€"],
    ["Digit6", "6", "&"],
    ["Digit7", "7", "/", "{"],
    ["Digit8", "8", "(", "["],
    ["Digit9", "9", ")", "]"],
    ["Digit0", "0", "=", "}"],
    ["Minus", "+", "?", "\\"],
  ];

  // Each row: [DOM code, base char, shifted char?, AltGr char?]
  const CONSOLE_KEY_ROWS = {
    "en-us": [
      ...CONSOLE_LETTER_ROWS,
      ["Digit1", "1", "!"],
      ["Digit2", "2", "@"],
      ["Digit3", "3", "#"],
      ["Digit4", "4", "$"],
      ["Digit5", "5", "%"],
      ["Digit6", "6", "^"],
      ["Digit7", "7", "&"],
      ["Digit8", "8", "*"],
      ["Digit9", "9", "("],
      ["Digit0", "0", ")"],
      ["Minus", "-", "_"],
      ["Equal", "=", "+"],
      ["BracketLeft", "[", "{"],
      ["BracketRight", "]", "}"],
      ["Backslash", "\\", "|"],
      ["Semicolon", ";", ":"],
      ["Quote", "'", '"'],
      ["Backquote", "`", "~"],
      ["Comma", ",", "<"],
      ["Period", ".", ">"],
      ["Slash", "/", "?"],
      ["Space", " "],
    ],
    "en-gb": [
      ...CONSOLE_LETTER_ROWS,
      ["Digit1", "1", "!"],
      ["Digit2", "2", '"'],
      ["Digit3", "3", "£"],
      ["Digit4", "4", "$", "€"],
      ["Digit5", "5", "%"],
      ["Digit6", "6", "^"],
      ["Digit7", "7", "&"],
      ["Digit8", "8", "*"],
      ["Digit9", "9", "("],
      ["Digit0", "0", ")"],
      ["Minus", "-", "_"],
      ["Equal", "=", "+"],
      ["BracketLeft", "[", "{"],
      ["BracketRight", "]", "}"],
      ["Backslash", "#", "~"],
      ["Semicolon", ";", ":"],
      ["Quote", "'", "@"],
      ["Backquote", "`", "¬"],
      ["Comma", ",", "<"],
      ["Period", ".", ">"],
      ["Slash", "/", "?"],
      ["IntlBackslash", "\\", "|"],
      ["Space", " "],
    ],
    de: [
      ...CONSOLE_LETTER_ROWS_DE,
      ["Digit1", "1", "!"],
      ["Digit2", "2", '"'],
      ["Digit3", "3", "§"],
      ["Digit4", "4", "$"],
      ["Digit5", "5", "%"],
      ["Digit6", "6", "&"],
      ["Digit7", "7", "/", "{"],
      ["Digit8", "8", "(", "["],
      ["Digit9", "9", ")", "]"],
      ["Digit0", "0", "=", "}"],
      ["Minus", "ß", "?", "\\"],
      ["BracketLeft", "ü", "Ü"],
      ["BracketRight", "+", "*", "~"],
      ["Semicolon", "ö", "Ö"],
      ["Quote", "ä", "Ä"],
      ["Backslash", "#", "'"],
      ["IntlBackslash", "<", ">", "|"],
      ["Comma", ",", ";"],
      ["Period", ".", ":"],
      ["Slash", "-", "_"],
      ["Space", " "],
    ],
    sv: [
      ...CONSOLE_LETTER_ROWS,
      ...CONSOLE_NORDIC_DIGITS,
      ["BracketLeft", "å", "Å"],
      ["Semicolon", "ö", "Ö"],
      ["Quote", "ä", "Ä"],
      ["Backslash", "'", "*"],
      ["IntlBackslash", "<", ">", "|"],
      ["Comma", ",", ";"],
      ["Period", ".", ":"],
      ["Slash", "-", "_"],
      ["Backquote", "§", "½"],
      ["Space", " "],
    ],
    no: [
      ...CONSOLE_LETTER_ROWS,
      ...CONSOLE_NORDIC_DIGITS,
      ["BracketLeft", "å", "Å"],
      ["Semicolon", "ø", "Ø"],
      ["Quote", "æ", "Æ"],
      ["Backslash", "'", "*"],
      ["IntlBackslash", "<", ">"],
      ["Comma", ",", ";"],
      ["Period", ".", ":"],
      ["Slash", "-", "_"],
      ["Backquote", "|", "§"],
      ["Space", " "],
    ],
    da: [
      ...CONSOLE_LETTER_ROWS,
      ...CONSOLE_NORDIC_DIGITS,
      ["BracketLeft", "å", "Å"],
      ["Semicolon", "æ", "Æ"],
      ["Quote", "ø", "Ø"],
      ["Backslash", "'", "*"],
      ["IntlBackslash", "<", ">", "\\"],
      ["Comma", ",", ";"],
      ["Period", ".", ":"],
      ["Slash", "-", "_"],
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
          localStorage.setItem(reconnectKey, JSON.stringify({ until: Date.now() + keepaliveMinutes() * 60 * 1000 }));
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
          script.addEventListener(
            "load",
            () => {
              script.dataset.loaded = "true";
              resolve();
            },
            { once: true }
          );
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
        rfb = new RFB(screen, buildConsoleWebSocketUrl(payload).href, {
          credentials: { password: payload.password || "" },
        });
        applySettings();
        rfb.focusOnClick = true;
        rfb.addEventListener("connect", markConnected);
        rfb.addEventListener("disconnect", (event) => {
          rfb = null;
          const clean = event.detail?.clean;
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
        mods.forEach((code) => {
          rfb.sendKey(CONSOLE_MODIFIER_KEYSYMS[code], code, true);
        });
        rfb.sendKey(keysym, spec.code, true);
        rfb.sendKey(keysym, spec.code, false);
        mods
          .slice()
          .reverse()
          .forEach((code) => {
            rfb.sendKey(CONSOLE_MODIFIER_KEYSYMS[code], code, false);
          });
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
          const targetPanel = page.querySelector(
            `[data-console-panel="${CSS.escape(button.dataset.consolePanelToggle || "")}"]`
          );
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

  const initCopyButtons = (root = document) => {
    root.querySelectorAll("[data-copy-command]").forEach((button) => {
      if (button.dataset.copyInit === "true") {
        return;
      }
      button.dataset.copyInit = "true";
      button.addEventListener("click", async () => {
        const code = button.closest(".health-command")?.querySelector("[data-health-command]");
        const text = (code?.textContent || "").trim();
        if (!text) {
          return;
        }
        const original = button.textContent;
        try {
          await navigator.clipboard.writeText(text);
          button.textContent = "Copied";
          window.setTimeout(() => {
            button.textContent = original;
          }, 1500);
        } catch (_error) {
          // Non-secure context or no clipboard API — select the text so the user can copy manually.
          const range = document.createRange();
          range.selectNodeContents(code);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
        }
      });
    });
  };

  const initPage = (root = document) => {
    initHardwareEditor(root);
    initVmRegister(root);
    initGuestActionForms(root);
    initCopyButtons(root);
    initBackupRestoreForms(root);
    initGuestListFilter(root);
    sortGuestList(document.documentElement.dataset.guestNameStyle !== "name-only");
    initNodeReload(root);
    initSummaryCards(root);
    initAutoSubmitForms(root);
    initAuditExportDialog(root);
    initScanActions(root);
    initStorageFileManagers(root);
    initConfirmedFileActions(root);
    initConfirmForms(root);
    initScheduledTaskForms(root);
    initScheduledRuns(root);
    initSpaceCharts(root);
    initTableFilters(root);
    initColumnPickers(root);
    initResizableColumns(root);
    initSortableTables(root);
    initVmOverviewSelection(root);
    initVmOverviewAgentInfo(root);
    initVmOverviewSnapshotInfo(root);
    initVmStatusRefresh(root);
    initGuestAgentSummaries(root);
    initConsolePages(root);
    applyIpVersionStyle(document.documentElement.dataset.ipVersionStyle || "all");
    createIcons();
  };

  const initShell = () => {
    applyTheme(preferredTheme());
    applyGuestNameStyle(preferredGuestNameStyle());
    applyIpVersionStyle(preferredIpVersionStyle());
    try {
      applyTaskbarState(localStorage.getItem(taskbarKey) === "true");
    } catch (_error) {
      applyTaskbarState(false);
    }
    try {
      applySidebarState(localStorage.getItem(sidebarCollapsedKey) === "true");
    } catch (_error) {
      applySidebarState(false);
    }

    initThemeToggle();
    initGuestNameToggle();
    initIpVersionToggle();
    initTaskbarToggle();
    initSidebarControls();
    initGlobalSearch();
    initTreeModules(document);
    initContextMenu();
    initSoftNavigation();
    initPage(document);
    initRecentTasks();
  };

  initShell();
})();
