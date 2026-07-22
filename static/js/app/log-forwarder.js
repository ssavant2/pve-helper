import { registerPageCleanup } from "./shell.js";

const initLogForwarderStatus = (root = document) => {
  root.querySelectorAll("[data-log-forwarder-status]").forEach((panel) => {
    if (panel.dataset.statusInitialized === "true") return;
    panel.dataset.statusInitialized = "true";

    const statusUrl = panel.dataset.statusUrl;
    const pollMs = Number.parseInt(panel.dataset.statusPollMs || "5000", 10);
    if (!statusUrl) return;

    const refresh = async () => {
      try {
        const response = await fetch(new URL(statusUrl, window.location.origin), {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const status = await response.json();
        panel.querySelector("[data-log-forwarder-state]").textContent = status.state;
        panel.querySelector("[data-log-forwarder-pending]").textContent = status.pending_label;
        panel.querySelector("[data-log-forwarder-last-delivery]").textContent = status.last_delivery;
        panel.querySelector("[data-log-forwarder-last-error]").textContent = status.last_error;
        panel.querySelector("[data-log-forwarder-paused]").hidden = !status.paused;
      } catch (_error) {
        // Keep the last known status visible while the next poll is pending.
      }
    };

    refresh();
    const intervalId = window.setInterval(
      () => {
        if (document.visibilityState !== "hidden") refresh();
      },
      Number.isFinite(pollMs) ? pollMs : 5000
    );
    registerPageCleanup(() => window.clearInterval(intervalId));
  });
};

export { initLogForwarderStatus };
