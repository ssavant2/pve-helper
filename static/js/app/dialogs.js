import { escapeHtml } from "./shell.js";

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

// Shared confirm/consequence dialog. `body` is trusted HTML; callers must
// escape user- or database-provided text before passing it.
/**
 * @param swapActions Render the declining button where Confirm normally sits.
 * Used for the second step of an escalated confirmation so a memorised
 * double-click on the same spot cannot carry an operator through both dialogs.
 * @param cancelLabel Override when declining is itself a recorded decision
 * rather than a way out. "Cancel" promises that nothing happens; if the button
 * durably answers a question, say what it answers.
 */
const openConfirmDialog = ({
  title = "Please confirm",
  body = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  swapActions = false,
}) =>
  new Promise((resolve) => {
    const dialog = ensureVmActionDialog();
    let decided = false;
    dialog.innerHTML = `
      <div class="vm-action-dialog-form">
        <div class="vm-action-dialog-heading">
          <h2>${escapeHtml(title)}</h2>
          <button type="button" data-confirm-dismiss aria-label="Close">×</button>
        </div>
        <div class="vm-action-dialog-body">${body}</div>
        <div class="form-actions">
          ${
            swapActions
              ? `<button class="primary-action" type="button" data-confirm-no>${escapeHtml(cancelLabel)}</button>
          <button class="secondary-action${danger ? " danger-action" : ""}" type="button" data-confirm-yes>${escapeHtml(confirmLabel)}</button>`
              : `<button class="primary-action${danger ? " danger-action" : ""}" type="button" data-confirm-yes>${escapeHtml(confirmLabel)}</button>
          <button class="secondary-action" type="button" data-confirm-no>${escapeHtml(cancelLabel)}</button>`
          }
        </div>
      </div>
    `;
    const finish = (result) => {
      if (decided) return;
      decided = true;
      resolve(result);
      dialog.close();
    };
    dialog.querySelector("[data-confirm-yes]")?.addEventListener("click", () => finish(true));
    dialog.querySelector("[data-confirm-no]")?.addEventListener("click", () => finish(false));
    dialog.querySelector("[data-confirm-dismiss]")?.addEventListener("click", () => finish(false));
    dialog.addEventListener("close", () => finish(false), { once: true });
    dialog.showModal?.();
  });

// Shared text-input dialog. Validation stays in the active dialog rather than
// falling back to a browser popup after the dialog has closed.
const openInputDialog = ({ title = "Enter a value", label = "", value = "", confirmLabel = "OK", validate = null }) =>
  new Promise((resolve) => {
    const dialog = ensureVmActionDialog();
    let decided = false;
    dialog.innerHTML = `
      <form class="vm-action-dialog-form" method="dialog">
        <div class="vm-action-dialog-heading">
          <h2>${escapeHtml(title)}</h2>
          <button type="button" data-input-dismiss aria-label="Close">×</button>
        </div>
        <label class="form-field">
          ${label ? `<span>${escapeHtml(label)}</span>` : ""}
          <input type="text" data-input-value autocomplete="off" value="${escapeHtml(value)}">
        </label>
        <p class="form-error" data-input-error role="alert" hidden></p>
        <div class="form-actions">
          <button class="primary-action" type="submit">${escapeHtml(confirmLabel)}</button>
          <button class="secondary-action" type="button" data-input-cancel>Cancel</button>
        </div>
      </form>
    `;
    const field = dialog.querySelector("[data-input-value]");
    const error = dialog.querySelector("[data-input-error]");
    const finish = (result) => {
      if (decided) return;
      decided = true;
      resolve(result);
      dialog.close();
    };
    dialog.querySelector("form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const nextValue = (field?.value ?? "").trim();
      const validationError = typeof validate === "function" ? validate(nextValue) : "";
      if (validationError) {
        if (error) {
          error.textContent = validationError;
          error.hidden = false;
        }
        field?.focus();
        return;
      }
      finish(nextValue || null);
    });
    dialog.querySelector("[data-input-cancel]")?.addEventListener("click", () => finish(null));
    dialog.querySelector("[data-input-dismiss]")?.addEventListener("click", () => finish(null));
    dialog.addEventListener("close", () => finish(null), { once: true });
    dialog.showModal?.();
    field?.focus();
  });

export { ensureVmActionDialog, openConfirmDialog, openInputDialog };
