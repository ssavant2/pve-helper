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
  if (url.pathname.startsWith("/auth/") || url.pathname.includes("/download/")) {
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
  document.getElementById("context-menu")?.setAttribute("hidden", "");

  navigationController?.abort();
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
    loadSoftNavigation(new URL(anchor.href, window.location.href));
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
