import { escapeHtml } from "./shell.js";

/**
 * Build a modal element for one single use and drop it when it closes.
 *
 * **Never go back to sharing one element between dialogs.** It reads as an
 * obvious economy and it breaks every chained confirmation in the application,
 * silently. `dialog.close()` does not fire `close` synchronously — the HTML
 * specification queues it as an element task — so a dialog opened in the
 * awaited continuation of the previous one is already on screen by the time the
 * *previous* dialog's close event is delivered. On a shared element that event
 * reaches the new dialog's handler, which has no way to tell it apart from the
 * operator pressing Escape, and resolves as a dismissal.
 *
 * What that cost: the risk question after Rename never appeared, the second
 * question before a permanent delete never appeared, and in both cases the
 * action was abandoned with no dialog, no request and no error — the button
 * simply did nothing. A per-call element makes the stale event land on a
 * detached node nobody listens to, which is the only fix that does not depend
 * on reasoning about task ordering staying correct forever.
 * `DialogModuleInvariantTests` fails if the shared element comes back.
 */
const createActionDialog = () => {
  const dialog = document.createElement("dialog");
  dialog.className = "vm-action-dialog";
  dialog.dataset.vmActionDialog = "true";
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
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
 * @param distinguishDismiss Resolve `"confirm"` / `"decline"` / `"dismiss"`
 * instead of a boolean, so the caller can tell an answer from a close.
 *
 * An ordinary confirmation needs no such distinction: declining and closing both
 * mean the action does not happen. It matters only where declining is itself a
 * durable decision, because then the × and Esc — which universally mean "I am not
 * deciding right now" — must not be allowed to decide. Callers that leave this
 * off keep the boolean contract, where any exit but Confirm is falsy.
 */
const openConfirmDialog = ({
  title = "Please confirm",
  body = "",
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  swapActions = false,
  distinguishDismiss = false,
}) =>
  new Promise((resolve) => {
    const dialog = createActionDialog();
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
    const finish = (outcome) => {
      if (decided) return;
      decided = true;
      resolve(distinguishDismiss ? outcome : outcome === "confirm");
      dialog.close();
    };
    dialog.querySelector("[data-confirm-yes]")?.addEventListener("click", () => finish("confirm"));
    dialog.querySelector("[data-confirm-no]")?.addEventListener("click", () => finish("decline"));
    dialog.querySelector("[data-confirm-dismiss]")?.addEventListener("click", () => finish("dismiss"));
    // Esc and a backdrop click land here too, and mean the same as the ×.
    dialog.addEventListener("close", () => finish("dismiss"), { once: true });
    dialog.showModal?.();
  });

// Shared text-input dialog. Validation stays in the active dialog rather than
// falling back to a browser popup after the dialog has closed.
const openInputDialog = ({ title = "Enter a value", label = "", value = "", confirmLabel = "OK", validate = null }) =>
  new Promise((resolve) => {
    const dialog = createActionDialog();
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

/**
 * Open a trusted-HTML consequence dialog with multiple text fields.
 *
 * Each field accepts `name`, `label`, `type` (`"text"` or `"textarea"`),
 * `value`, `placeholder`, `autocomplete`, `required`, `maxLength`, `rows`,
 * `hint`, `trim` and a `validate(value, values)` callback. The dialog-level
 * `validate(values)` callback can enforce relationships between fields. All
 * validation errors stay inside the active dialog.
 *
 * The ordinary result is the submitted values object, or `null` when the
 * operator declines or dismisses. With `distinguishDismiss`, the result is
 * `{ outcome: "confirm" | "decline" | "dismiss", values }`; `values` is only
 * populated for a confirmed outcome. `body` is trusted HTML on the same terms as
 * openConfirmDialog.
 */
const openFieldsDialog = ({
  title = "Please confirm",
  body = "",
  fields = [],
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  danger = false,
  swapActions = false,
  distinguishDismiss = false,
  validate = null,
}) => {
  const names = new Set();
  const fieldDefinitions = fields.map((field, index) => {
    const name = String(field.name ?? "").trim();
    const type = field.type ?? "text";
    if (!name || names.has(name)) {
      throw new TypeError("Dialog field names must be present and unique.");
    }
    if (type !== "text" && type !== "textarea") {
      throw new TypeError(`Unsupported dialog field type at index ${index}.`);
    }
    names.add(name);
    return {
      ...field,
      name,
      type,
      label: String(field.label ?? name),
      value: String(field.value ?? ""),
      trim: field.trim !== false,
    };
  });

  return new Promise((resolve) => {
    const dialog = createActionDialog();
    let decided = false;
    const fieldMarkup = fieldDefinitions
      .map((field, index) => {
        const commonAttributes = [
          `name="${escapeHtml(field.name)}"`,
          `data-fields-value="${index}"`,
          field.required ? 'aria-required="true"' : "",
          Number.isInteger(field.maxLength) && field.maxLength >= 0 ? `maxlength="${field.maxLength}"` : "",
          field.placeholder ? `placeholder="${escapeHtml(field.placeholder)}"` : "",
          field.autocomplete ? `autocomplete="${escapeHtml(field.autocomplete)}"` : 'autocomplete="off"',
        ]
          .filter(Boolean)
          .join(" ");
        const control =
          field.type === "textarea"
            ? `<textarea ${commonAttributes} rows="${Number.isInteger(field.rows) && field.rows > 0 ? field.rows : 3}">${escapeHtml(field.value)}</textarea>`
            : `<input type="text" ${commonAttributes} value="${escapeHtml(field.value)}">`;
        return `
          <label class="form-field">
            <span>${escapeHtml(field.label)}</span>
            ${control}
            ${field.hint ? `<small class="form-hint">${escapeHtml(field.hint)}</small>` : ""}
            <small class="form-error" data-fields-field-error="${index}" role="alert" hidden></small>
          </label>
        `;
      })
      .join("");

    dialog.innerHTML = `
      <form class="vm-action-dialog-form" method="dialog" novalidate>
        <div class="vm-action-dialog-heading">
          <h2>${escapeHtml(title)}</h2>
          <button type="button" data-fields-dismiss aria-label="Close">×</button>
        </div>
        <div class="vm-action-dialog-body">${body}</div>
        <div data-fields-fields>${fieldMarkup}</div>
        <p class="form-error" data-fields-error role="alert" hidden></p>
        <div class="form-actions">
          ${
            swapActions
              ? `<button class="primary-action" type="button" data-fields-no>${escapeHtml(cancelLabel)}</button>
          <button class="secondary-action${danger ? " danger-action" : ""}" type="submit" data-fields-yes>${escapeHtml(confirmLabel)}</button>`
              : `<button class="primary-action${danger ? " danger-action" : ""}" type="submit" data-fields-yes>${escapeHtml(confirmLabel)}</button>
          <button class="secondary-action" type="button" data-fields-no>${escapeHtml(cancelLabel)}</button>`
          }
        </div>
      </form>
    `;

    const controls = fieldDefinitions.map((_field, index) => dialog.querySelector(`[data-fields-value="${index}"]`));
    const formError = dialog.querySelector("[data-fields-error]");
    const finish = (outcome, values = null) => {
      if (decided) return;
      decided = true;
      resolve(distinguishDismiss ? { outcome, values: outcome === "confirm" ? values : null } : values);
      dialog.close();
    };
    const showFieldError = (index, message) => {
      const control = controls[index];
      const error = dialog.querySelector(`[data-fields-field-error="${index}"]`);
      control?.setAttribute("aria-invalid", "true");
      if (error) {
        error.textContent = message;
        error.hidden = false;
      }
    };
    const clearErrors = () => {
      controls.forEach((control, index) => {
        control?.removeAttribute("aria-invalid");
        const error = dialog.querySelector(`[data-fields-field-error="${index}"]`);
        if (error) {
          error.textContent = "";
          error.hidden = true;
        }
      });
      if (formError) {
        formError.textContent = "";
        formError.hidden = true;
      }
    };

    dialog.querySelector("form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      clearErrors();
      const values = Object.fromEntries(
        fieldDefinitions.map((field, index) => {
          const value = controls[index]?.value ?? "";
          return [field.name, field.trim ? value.trim() : value];
        })
      );
      let firstInvalid = -1;
      fieldDefinitions.forEach((field, index) => {
        const value = values[field.name];
        let validationError = "";
        if (field.required && !value) {
          validationError = `${field.label} is required.`;
        } else if (Number.isInteger(field.maxLength) && field.maxLength >= 0 && value.length > field.maxLength) {
          validationError = `${field.label} must be at most ${field.maxLength} characters.`;
        } else if (typeof field.validate === "function") {
          validationError = field.validate(value, values) || "";
        }
        if (validationError) {
          showFieldError(index, validationError);
          if (firstInvalid < 0) firstInvalid = index;
        }
      });
      if (firstInvalid >= 0) {
        controls[firstInvalid]?.focus();
        return;
      }
      const validationError = typeof validate === "function" ? validate(values) : "";
      if (validationError) {
        if (formError) {
          formError.textContent = validationError;
          formError.hidden = false;
        }
        controls[0]?.focus();
        return;
      }
      finish("confirm", values);
    });
    dialog.querySelector("[data-fields-no]")?.addEventListener("click", () => finish("decline"));
    dialog.querySelector("[data-fields-dismiss]")?.addEventListener("click", () => finish("dismiss"));
    dialog.addEventListener("close", () => finish("dismiss"), { once: true });
    dialog.showModal?.();
    controls[0]?.focus();
  });
};

/**
 * Report an outcome where the operator's flow was, rather than behind it.
 *
 * A refusal that arrives as a banner on the page underneath is a refusal the
 * operator has to go looking for: they answered a question in a modal, the modal
 * closed, and nothing visibly happened. `body` is trusted HTML on the same terms
 * as openConfirmDialog.
 */
const openNoticeDialog = ({ title = "Action failed", body = "", closeLabel = "Close" }) =>
  new Promise((resolve) => {
    const dialog = createActionDialog();
    let done = false;
    dialog.innerHTML = `
      <div class="vm-action-dialog-form">
        <div class="vm-action-dialog-heading">
          <h2>${escapeHtml(title)}</h2>
          <button type="button" data-notice-dismiss aria-label="Close">×</button>
        </div>
        <div class="vm-action-dialog-body">${body}</div>
        <div class="form-actions">
          <button class="primary-action" type="button" data-notice-close>${escapeHtml(closeLabel)}</button>
        </div>
      </div>
    `;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
      dialog.close();
    };
    dialog.querySelector("[data-notice-close]")?.addEventListener("click", finish);
    dialog.querySelector("[data-notice-dismiss]")?.addEventListener("click", finish);
    dialog.addEventListener("close", finish, { once: true });
    dialog.showModal?.();
    dialog.querySelector("[data-notice-close]")?.focus();
  });

export { createActionDialog, openConfirmDialog, openFieldsDialog, openInputDialog, openNoticeDialog };
