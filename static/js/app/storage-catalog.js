/**
 * The datastore header's Refresh button.
 *
 * **This must stay part of the module shell.** It used to be a bare
 * `<script src>` inside `{% block content %}`, and soft navigation replaces that
 * block with `innerHTML`, which never executes a script. So after the first soft
 * navigation — which is every file action — the form had no listener at all, the
 * global submit handler POSTed it as a navigation, got the refresh endpoint's
 * JSON back, and fell through to a full page load. The button that was meant to
 * refresh one panel reloaded the whole page instead.
 */

const setStatus = (status, text) => {
  if (status) {
    status.textContent = text;
  }
};

const initStorageCatalogRefresh = (root = document) => {
  root.querySelectorAll("[data-storage-catalog-refresh]").forEach((form) => {
    if (form.dataset.initialized === "true") {
      return;
    }
    form.dataset.initialized = "true";

    const button = form.querySelector("button");
    const status = form.querySelector("[data-refresh-status]");

    form.addEventListener("submit", async (event) => {
      // The response is a task acknowledgement, not a page; letting the shell's
      // navigation handler see this submit is what caused the hard reload.
      event.preventDefault();
      event.stopPropagation();
      if (button) {
        button.disabled = true;
      }
      setStatus(status, "Queueing…");
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) {
          setStatus(status, payload.error || "Refresh could not be queued");
        } else if (payload.status === "already-running") {
          setStatus(status, "Refresh already running");
        } else {
          // The durable task row owns the outcome from here: Recent Tasks shows
          // it, and this page re-renders itself when it lands.
          setStatus(status, "Refresh queued");
        }
      } catch (_error) {
        setStatus(status, "Refresh could not be queued");
      } finally {
        if (button) {
          button.disabled = false;
        }
      }
    });
  });
};

export { initStorageCatalogRefresh };
