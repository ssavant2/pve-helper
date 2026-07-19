document.querySelectorAll("[data-storage-catalog-refresh]").forEach((form) => {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button");
    const status = form.querySelector("[data-refresh-status]");
    button.disabled = true;
    status.textContent = "Queueing…";
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      status.textContent = response.ok ? "Refresh queued" : "Refresh could not be queued";
    } catch (_error) {
      status.textContent = "Refresh could not be queued";
    } finally {
      button.disabled = false;
    }
  });
});
