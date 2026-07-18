// Client-side cluster view switcher for the VM/CT inventory and overview.
//
// Both surfaces already render every cluster's rows with a `data-guest-cluster`
// attribute, so filtering by cluster is a pure show/hide over rows already in the
// DOM — no reload, instant switch. Non-matching rows (and the inventory's
// per-cluster group headings) get the shared `cluster-hidden` class, which is
// display:none. That composes with the text quick-filter, which hides via the
// `hidden` attribute: a row is visible only when neither mechanism hides it.
//
// The selection is stored so it survives navigation between the two views and
// page reloads. The control is only rendered when more than one cluster is
// enabled, so a single-cluster install never sees it.

const STORAGE_KEY = "pve-helper:cluster-filter";

const readStored = () => {
  try {
    return localStorage.getItem(STORAGE_KEY) || "";
  } catch (_error) {
    return "";
  }
};

const writeStored = (value) => {
  try {
    if (value) {
      localStorage.setItem(STORAGE_KEY, value);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  } catch (_error) {
    // Storage unavailable (private mode): filtering still works for this view.
  }
};

const applyClusterFilter = (key) => {
  const hide = (el, attr) => {
    el.classList.toggle("cluster-hidden", Boolean(key) && el.getAttribute(attr) !== key);
  };
  document.querySelectorAll("[data-guest-cluster]").forEach((el) => {
    hide(el, "data-guest-cluster");
  });
  document.querySelectorAll("[data-guest-cluster-group]").forEach((el) => {
    hide(el, "data-guest-cluster-group");
  });
  document.querySelectorAll("[data-cluster-filter-control]").forEach((control) => {
    control.querySelectorAll("[data-cluster-filter]").forEach((button) => {
      const active = (button.getAttribute("data-cluster-filter") || "") === (key || "");
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", active ? "true" : "false");
    });
  });
  document.dispatchEvent(new CustomEvent("pve-helper:cluster-filter-changed", { detail: { clusterKey: key || "" } }));
};

const initClusterFilter = (root = document) => {
  const controls = root.querySelectorAll("[data-cluster-filter-control]");
  if (!controls.length) {
    return;
  }

  // A stored cluster may no longer be enabled; fall back to All rather than
  // hiding every row against a key that no button offers.
  const available = new Set();
  controls.forEach((control) => {
    control.querySelectorAll("[data-cluster-filter]").forEach((button) => {
      available.add(button.getAttribute("data-cluster-filter") || "");
    });
  });
  let key = readStored();
  if (key && !available.has(key)) {
    key = "";
    writeStored("");
  }

  controls.forEach((control) => {
    if (control.dataset.initialized === "true") {
      return;
    }
    control.dataset.initialized = "true";
    control.addEventListener("click", (event) => {
      const button = event.target.closest("[data-cluster-filter]");
      if (!button) {
        return;
      }
      const selected = button.getAttribute("data-cluster-filter") || "";
      writeStored(selected);
      applyClusterFilter(selected);
    });
  });

  applyClusterFilter(key);
};

export { initClusterFilter };
