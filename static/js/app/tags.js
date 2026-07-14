import { loadSoftNavigation, openConfirmDialog } from "./guest-actions.js";
import { escapeHtml } from "./shell.js";

const initTags = (root = document) => {
  const refreshCurrentTagView = async () => {
    const url = new URL(window.location.href);
    url.searchParams.set("_tag_refresh", Date.now().toString());
    await loadSoftNavigation(url, { push: false });
  };

  root.querySelectorAll("[data-tag-refresh-form]").forEach((form) => {
    if (form.dataset.initialized === "true") return;
    form.dataset.initialized = "true";
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = form.querySelector('button[type="submit"]');
      const error = form.querySelector("[data-tag-refresh-error]");
      submit.disabled = true;
      if (error) error.hidden = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: new FormData(form),
          headers: { Accept: "application/json", "X-Requested-With": "fetch" },
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok || !payload.ok) {
          throw new Error(payload.error || `HTTP ${response.status}`);
        }
        submit.textContent = "Refresh queued";
        window.pveHelperRefreshRecentTasks?.();
      } catch (requestError) {
        submit.disabled = false;
        if (error) {
          error.textContent = requestError.message || "The tag inventory refresh could not be queued.";
          error.hidden = false;
        }
      }
    });
  });

  root
    .querySelectorAll('form[action*="/tags/"] input[name="tag"], form[action*="/tags/"] input[name="new_tag"]')
    .forEach((input) => {
      input.addEventListener("input", () => {
        input.value = input.value.toLowerCase().replaceAll(" ", "-");
      });
    });

  root.querySelectorAll("[data-tag-async-form]").forEach((asyncForm) => {
    if (asyncForm.dataset.initialized === "true") return;
    asyncForm.dataset.initialized = "true";
    asyncForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const error = asyncForm.querySelector("[data-tag-form-error]");
      const submit = asyncForm.querySelector('button[type="submit"]');
      submit.disabled = true;
      if (error) error.hidden = true;
      try {
        const response = await fetch(asyncForm.action, {
          method: "POST",
          body: new FormData(asyncForm),
          headers: { Accept: "application/json", "X-Requested-With": "fetch" },
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        window.pveHelperRefreshRecentTasks?.();
        await loadSoftNavigation(new URL(window.location.href), { push: false });
      } catch (requestError) {
        if (error) {
          error.textContent = requestError.message || "The tag could not be saved.";
          error.hidden = false;
        }
      } finally {
        submit.disabled = false;
      }
    });
  });

  root.querySelectorAll("[data-guest-tag-editor]").forEach((editor) => {
    if (editor.dataset.initialized === "true") return;
    editor.dataset.initialized = "true";
    const hidden = editor.querySelector('[name="tags"]');
    const select = editor.querySelector("[data-existing-tag-select]");
    const add = editor.querySelector("[data-existing-tag-add]");
    const selectedList = editor.querySelector("[data-selected-tags]");
    const newTag = editor.querySelector('[name="new_tag"]');
    const selected = new Set(
      String(hidden?.value || "")
        .split(/[;,\s]+/)
        .filter(Boolean)
    );
    const render = () => {
      hidden.value = Array.from(selected).join(";");
      selectedList.innerHTML = "";
      Array.from(selected)
        .sort()
        .forEach((name) => {
          const chip = document.createElement("span");
          chip.className = "guest-tag-selection";
          chip.append(document.createTextNode(name));
          const remove = document.createElement("button");
          remove.type = "button";
          remove.setAttribute("aria-label", `Remove ${name}`);
          remove.textContent = "×";
          remove.addEventListener("click", () => {
            selected.delete(name);
            render();
          });
          chip.append(remove);
          selectedList.append(chip);
        });
      Array.from(select.options).forEach((option) => {
        option.disabled = selected.has(option.value);
      });
      select.value = "";
    };
    add?.addEventListener("click", () => {
      if (select.value) {
        selected.add(select.value);
        render();
      }
    });
    newTag?.addEventListener("input", () => {
      newTag.value = newTag.value.toLowerCase().replaceAll(" ", "-");
    });
    render();
  });

  root.querySelectorAll("[data-tag-unassign-form]").forEach((unassignForm) => {
    if (unassignForm.dataset.initialized === "true") return;
    unassignForm.dataset.initialized = "true";
    unassignForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const tag = unassignForm.dataset.tag || "tag";
      const guest = unassignForm.dataset.guestLabel || "object";
      const confirmed = await openConfirmDialog({
        title: "Remove tag",
        body: `<p>Remove <strong>${escapeHtml(tag)}</strong> from <strong>${escapeHtml(guest)}</strong>?</p>`,
        confirmLabel: "Remove tag",
        danger: true,
      });
      if (!confirmed) return;

      const submit = unassignForm.querySelector('button[type="submit"]');
      const error = root.querySelector("[data-tag-unassign-error]");
      submit.disabled = true;
      if (error) error.hidden = true;
      try {
        const response = await fetch(unassignForm.action, {
          method: "POST",
          body: new FormData(unassignForm),
          headers: { Accept: "application/json", "X-Requested-With": "fetch" },
        });
        const payload = await response.json();
        if (!response.ok || !payload.ok) {
          throw new Error((payload.errors || [`HTTP ${response.status}`]).join("; "));
        }
        window.pveHelperRefreshRecentTasks?.();
        await refreshCurrentTagView();
      } catch (requestError) {
        if (error) {
          error.textContent = requestError.message || "Could not remove the tag from this object.";
          error.hidden = false;
        }
      } finally {
        submit.disabled = false;
      }
    });
  });

  const dialog = root.querySelector("[data-tag-assign-dialog]");
  const form = dialog?.querySelector("[data-tag-assign-form]");
  if (!dialog || !form || dialog.dataset.initialized === "true") return;
  dialog.dataset.initialized = "true";
  const close = () => dialog.close();
  root.querySelector("[data-tag-assign-open]")?.addEventListener("click", () => dialog.showModal());
  dialog.querySelector("[data-tag-assign-close]")?.addEventListener("click", close);
  dialog.querySelector("[data-tag-assign-cancel]")?.addEventListener("click", close);
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) close();
  });

  const rows = Array.from(dialog.querySelectorAll("[data-tag-assign-row]"));
  const filter = dialog.querySelector("[data-tag-assign-filter]");
  const selectAll = dialog.querySelector("[data-tag-assign-all]");
  filter?.addEventListener("input", () => {
    const query = filter.value.trim().toLowerCase();
    rows.forEach((row) => {
      row.hidden = Boolean(query) && !String(row.dataset.filterText || "").includes(query);
    });
  });
  selectAll?.addEventListener("change", () => {
    rows
      .filter((row) => !row.hidden)
      .forEach((row) => {
        row.querySelector('input[type="checkbox"]').checked = selectAll.checked;
      });
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const error = form.querySelector("[data-tag-assign-error]");
    const selected = form.querySelectorAll('input[name="guest"]:checked');
    if (!selected.length) {
      error.textContent = "Select at least one object.";
      error.hidden = false;
      return;
    }
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { Accept: "application/json", "X-Requested-With": "fetch" },
      });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error((payload.errors || [`HTTP ${response.status}`]).join("; "));
      }
      close();
      window.pveHelperRefreshRecentTasks?.();
      await loadSoftNavigation(new URL(window.location.href), { push: false });
    } catch (requestError) {
      error.textContent = requestError.message || "Could not assign the tag.";
      error.hidden = false;
    } finally {
      submit.disabled = false;
    }
  });
};

export { initTags };
