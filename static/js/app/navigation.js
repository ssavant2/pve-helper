import {
  createIcons,
  initTreeModules,
  refreshSidebarWidth,
  runPageCleanup,
  softContentSelector,
  softStatusSelector,
  softTreeSelector,
} from "./shell.js";

let navigationController = null;
let pageInitializer = () => {};

const setPageInitializer = (initializer) => {
  pageInitializer = typeof initializer === "function" ? initializer : () => {};
};

// Paths whose response is not an app page: session teardown (which may hand off
// to the identity provider) and anything that streams a file back.
const leavesTheApplication = (url) =>
  url.origin !== window.location.origin ||
  url.pathname.startsWith("/auth/") ||
  url.pathname.includes("/download/") ||
  url.pathname.includes("/export/");

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
  if (leavesTheApplication(url)) {
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
  pageInitializer(currentContent);
  createIcons();
  return true;
};

const loadSoftNavigation = async (url, options = {}) => {
  const push = options.push !== false;
  const method = (options.method || "get").toUpperCase();
  document.getElementById("context-menu")?.setAttribute("hidden", "");

  navigationController?.abort();
  const controller = new AbortController();
  navigationController = controller;
  setSoftNavigationLoading(true);

  try {
    const response = await fetch(url.href, {
      method,
      body: options.body,
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
    // A POST usually redirects; the address bar must show where we landed, not
    // where we posted. Landing back on the current page is an in-place update,
    // not a new history entry.
    const landedAt = response.redirected ? new URL(response.url) : url;
    if (push) {
      if (landedAt.href === window.location.href) {
        window.history.replaceState({ softNavigation: true }, "", landedAt.href);
      } else {
        window.history.pushState({ softNavigation: true }, "", landedAt.href);
      }
    }
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    // Re-issuing a failed POST as a GET would be wrong, and the request may
    // never have reached the server; reload where the user actually is.
    window.location.assign(method === "POST" ? window.location.href : url.href);
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
    loadSoftNavigation(new URL(anchor.href, window.location.href));
  });

  // Forms are navigations too. Without this, every template form that has no
  // JavaScript of its own reloads the whole document — losing scroll position,
  // the taskbar's polling state and every open panel — for what is usually a
  // single field edit. Feature modules that own a form call preventDefault
  // first and are skipped here; anything that genuinely must leave the app
  // (session teardown, a file download) opts out with data-no-soft-navigation,
  // the same marker links already use.
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form");
    if (!form || event.defaultPrevented || form.closest("[data-no-soft-navigation]")) {
      return;
    }
    if (form.hasAttribute("target") || form.hasAttribute("formtarget")) {
      return;
    }
    const action = new URL(form.getAttribute("action") || window.location.href, window.location.href);
    if (leavesTheApplication(action)) {
      return;
    }

    const method = (form.getAttribute("method") || "get").toLowerCase();
    if (method === "get") {
      // A filter/search form is just a link with a query string.
      event.preventDefault();
      action.search = new URLSearchParams(new FormData(form)).toString();
      loadSoftNavigation(action);
      return;
    }

    event.preventDefault();
    loadSoftNavigation(action, { method: "post", body: new FormData(form) });
  });

  document.addEventListener("change", (event) => {
    const selector = event.target.closest("[data-cluster-navigation]");
    if (!selector?.value) {
      return;
    }
    loadSoftNavigation(new URL(selector.value, window.location.href));
  });

  window.addEventListener("popstate", () => {
    loadSoftNavigation(new URL(window.location.href), { push: false });
  });
};

export {
  initSoftNavigation,
  loadSoftNavigation,
  replacePageFromDocument,
  setPageInitializer,
  setSoftNavigationLoading,
  shouldUseSoftNavigation,
};
