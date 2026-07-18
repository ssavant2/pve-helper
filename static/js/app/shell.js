import { updateVmRowStatus } from "./vm-overview.js";

// Shared application controllers. The module is bootstrapped by ../app.js.
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

let pageCleanup = [];
const activeUploads = new Map();

const ipVersion = (value) => (String(value || "").includes(":") ? "6" : "4");

const parseGuestRef = (value) => {
  const raw = String(value || "").trim();
  const [identity, node = ""] = raw.split("@", 2);
  const parts = identity.split(":");
  if (parts.length === 4 && parts[0] === "gr1") {
    return { cluster: parts[1], type: parts[2], vmid: parts[3], node };
  }
  if (parts.length === 2) {
    return { cluster: "", type: parts[0], vmid: parts[1], node };
  }
  return { cluster: "", type: "", vmid: "", node: "" };
};

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

const refreshGuestStateAfterTaskTransitions = (tasks, previousTaskStatuses) => {
  const terminalStatuses = new Set(["completed", "failed", "cancelled"]);
  const completedGuestTask = (tasks || []).some((task) => {
    if (!String(task.action || "").startsWith("guest.") || !terminalStatuses.has(task.status_class)) {
      return false;
    }
    const previousStatus = previousTaskStatuses.get(task.id);
    return Boolean(previousStatus && previousStatus !== task.status_class);
  });
  if (!completedGuestTask) {
    return;
  }
  document.querySelectorAll("[data-vm-overview]").forEach((overview) => {
    if (typeof overview.refreshVmStatus === "function") {
      overview.refreshVmStatus({ force: true });
      window.setTimeout(() => overview.refreshVmStatus({ force: true }), 1000);
    }
  });
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
// Reorder already-ordered rows so each linked clone sits directly under its
// parent template (recursively), preserving the incoming order among roots and
// among siblings. Rows carry data-guest-vmid / data-guest-parent-vmid. Used by
// the sidebar sort and the overview's name-column sort so a clone never lands
// at its own VMID away from its parent.
const groupRowsBySubtree = (rows) => {
  const byVmid = new Map();
  rows.forEach((row) => {
    const vmid = parseInt(row.dataset.guestVmid || "", 10);
    if (!Number.isNaN(vmid)) {
      byVmid.set(`${row.dataset.guestCluster || ""}:${vmid}`, row);
    }
  });
  const childrenByParent = new Map();
  const roots = [];
  rows.forEach((row) => {
    const parent = parseInt(row.dataset.guestParentVmid || "", 10);
    const parentKey = `${row.dataset.guestCluster || ""}:${parent}`;
    if (!Number.isNaN(parent) && byVmid.has(parentKey)) {
      if (!childrenByParent.has(parentKey)) {
        childrenByParent.set(parentKey, []);
      }
      childrenByParent.get(parentKey).push(row);
    } else {
      roots.push(row);
    }
  });
  const ordered = [];
  const emit = (row) => {
    ordered.push(row);
    const vmid = parseInt(row.dataset.guestVmid || "", 10);
    const rowKey = `${row.dataset.guestCluster || ""}:${vmid}`;
    (childrenByParent.get(rowKey) || []).forEach(emit);
  };
  roots.forEach(emit);
  return ordered;
};

const sortGuestList = (showingIds) => {
  const compare = (a, b) => {
    const clusterOrder = (a.dataset.guestCluster || "").localeCompare(b.dataset.guestCluster || "", undefined, {
      sensitivity: "base",
    });
    if (clusterOrder !== 0) return clusterOrder;
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
  };
  document.querySelectorAll("[data-guest-list]").forEach((list) => {
    const items = Array.from(list.querySelectorAll("[data-guest-target]"));
    if (items.length < 2) {
      return;
    }
    // Sort flat by the chosen key, then pull each clone back under its parent.
    items.sort(compare);
    const orderedItems = groupRowsBySubtree(items);
    const clusterHeaders = new Map(
      Array.from(list.querySelectorAll("[data-guest-cluster-group]")).map((header) => [
        header.dataset.guestClusterGroup || "",
        header,
      ])
    );
    const emittedClusters = new Set();
    orderedItems.forEach((item) => {
      const clusterKey = item.dataset.guestCluster || "";
      if (!emittedClusters.has(clusterKey)) {
        const header = clusterHeaders.get(clusterKey);
        if (header) list.appendChild(header);
        emittedClusters.add(clusterKey);
      }
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

export {
  activeUploads,
  addPendingRecentTask,
  applyGuestNameStyle,
  applyGuestStatusHintsFromTasks,
  applyIpVersionStyle,
  applySidebarState,
  applyTaskbarState,
  applyTheme,
  applyTreeModuleState,
  CLARITY_ICONS,
  clampSidebarWidth,
  consoleKeepaliveKey,
  consoleLayoutKey,
  consoleReconnectPrefix,
  createIcons,
  escapeHtml,
  expectedGuestStatusFromTask,
  groupRowsBySubtree,
  guestNameStyleKey,
  guestPowerActions,
  initGlobalSearch,
  initGuestNameToggle,
  initIpVersionToggle,
  initSidebarControls,
  initTaskbarToggle,
  initThemeToggle,
  initTreeModules,
  ipVersion,
  ipVersionStyleKey,
  measureSidebarExpandedWidth,
  measureSidebarMinimumWidth,
  parseGuestRef,
  preferredGuestNameStyle,
  preferredIpVersionStyle,
  preferredTheme,
  recentTasksRefreshEvent,
  refreshGuestStateAfterTaskTransitions,
  refreshSidebarWidth,
  registerPageCleanup,
  rememberSidebarWidth,
  renderGuestLabel,
  renderIpCell,
  renderVIcons,
  runPageCleanup,
  sidebarCollapsedKey,
  sidebarMaxWidth,
  sidebarWidthKey,
  softContentSelector,
  softStatusSelector,
  softTreeSelector,
  sortGuestList,
  storedSidebarWidth,
  taskbarKey,
  taskDateLabel,
  taskGuestTargetCandidates,
  themeKey,
  treeStateKey,
  updatePendingRecentTask,
  visibleIpText,
};
