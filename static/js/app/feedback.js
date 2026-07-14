const feedbackScope = (anchor) =>
  anchor?.closest?.("dialog[open], [data-storage-file-manager], [data-vm-overview], .panel, .taskbar") ||
  document.querySelector("main");

const clearLocalError = (anchor) => {
  const scope = feedbackScope(anchor);
  const error = scope?.querySelector("[data-local-action-error]");
  if (error) {
    error.hidden = true;
    error.textContent = "";
  }
};

const showLocalError = (anchor, message) => {
  const scope = feedbackScope(anchor);
  if (!scope) return;
  let error = scope.querySelector("[data-local-action-error]");
  if (!error) {
    error = document.createElement("p");
    error.className = "form-error action-feedback";
    error.dataset.localActionError = "true";
    error.setAttribute("role", "alert");
    const dialogActions = scope.matches("dialog") ? scope.querySelector(".form-actions") : null;
    const heading = scope.querySelector(":scope > .panel-heading, :scope > .taskbar-header");
    if (dialogActions) dialogActions.before(error);
    else if (heading) heading.after(error);
    else scope.prepend(error);
  }
  error.textContent = String(message || "The request failed.");
  error.hidden = false;
  error.tabIndex = -1;
  error.focus({ preventScroll: false });
};

export { clearLocalError, showLocalError };
