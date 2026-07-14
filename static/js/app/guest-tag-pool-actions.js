import { openVmFormDialog, selectedGuestSummary, submitVmBulkAction } from "./guest-actions.js";
import { escapeHtml } from "./shell.js";

const guestRowTags = (row) => {
  const metadataTags = String(row?.dataset.guestTags || "")
    .split(";")
    .map((tag) => tag.trim())
    .filter(Boolean);
  const renderedTags = Array.from(
    row?.querySelectorAll("[data-user-tag]") || [],
    (chip) => chip.dataset.userTag
  ).filter(Boolean);
  return [...new Set([...metadataTags, ...renderedTags])];
};

const availableGuestTags = (overview) => {
  const options = overview.querySelector("#vm-overview-tag-options");
  let registered = [];
  try {
    registered = JSON.parse(options?.textContent || "[]");
  } catch (_error) {
    // Tags rendered on the rows remain usable if registry metadata is stale.
  }
  const rendered = Array.from(overview.querySelectorAll("[data-vm-overview-row]")).flatMap(guestRowTags);
  return [...new Set([...registered, ...rendered])].sort((left, right) => left.localeCompare(right));
};

const tagChoicesForRows = (overview, rows, mode) => {
  if (mode === "remove") {
    return [...new Set(rows.flatMap(guestRowTags))].sort((left, right) => left.localeCompare(right));
  }
  return availableGuestTags(overview).filter((tag) => !rows.every((row) => guestRowTags(row).includes(tag)));
};

const liveTagChoicesForRows = async (overview, rows, mode) => {
  let loaded = false;
  const available = new Set();
  await Promise.all(
    rows.map(async (row) => {
      const url = row.dataset.tagOptionsUrl || "";
      if (!url) return;
      try {
        const response = await fetch(new URL(url, window.location.origin), {
          headers: { Accept: "application/json" },
        });
        if (!response.ok) return;
        const payload = await response.json();
        const assigned = Array.isArray(payload.assigned_tags) ? payload.assigned_tags : [];
        row.dataset.guestTags = assigned.join(";");
        (Array.isArray(payload.available_tags) ? payload.available_tags : []).forEach((tag) => {
          available.add(tag);
        });
        loaded = true;
      } catch (_error) {
        // Fall back to the rendered scan data if the live request fails.
      }
    })
  );
  if (!loaded) return tagChoicesForRows(overview, rows, mode);
  if (mode === "remove") {
    return [...new Set(rows.flatMap(guestRowTags))].sort((left, right) => left.localeCompare(right));
  }
  return [...available]
    .filter((tag) => !rows.every((row) => guestRowTags(row).includes(tag)))
    .sort((left, right) => left.localeCompare(right));
};

const openTagsDialog = async (overview, rows, mode) => {
  const adding = mode === "add";
  const choices = await liveTagChoicesForRows(overview, rows, mode);
  const options = choices.map((tag) => `<option value="${escapeHtml(tag)}">${escapeHtml(tag)}</option>`).join("");
  openVmFormDialog({
    title: adding ? "Add Tags" : "Remove Tags",
    summary: selectedGuestSummary(rows),
    submitLabel: adding ? "Add" : "Remove",
    bodyHtml: `
        <label class="form-field">
          <span>${adding ? "Available tag" : "Assigned tag"}</span>
          <select name="tags_value" ${choices.length ? "" : "disabled"}>
            ${options || `<option value="">${adding ? "No tags are available to add" : "No user tags are assigned"}</option>`}
          </select>
        </label>
      `,
    onSubmit: (formData) => {
      const tag = String(formData.get("tags_value") || "").trim();
      if (!tag) {
        return adding ? "Choose a tag to add." : "Choose a tag to remove.";
      }
      submitVmBulkAction(overview, "tags", { tags_mode: mode, tags_value: tag }, rows);
      return "";
    },
  });
};

const openPoolDialog = (overview, rows) => {
  const row = rows[0];
  const optionsUrl = row?.dataset.poolOptionsUrl || "";
  const dialog = openVmFormDialog({
    title: "Move to Pool",
    summary: selectedGuestSummary(rows),
    submitLabel: "Move",
    bodyHtml: `
        <label class="form-field">
          <span>Pool</span>
          <select name="pool_id" disabled>
            <option value="">Loading pools...</option>
          </select>
        </label>
        <p class="form-hint" data-pool-current hidden></p>
      `,
    onSubmit: (formData) => {
      submitVmBulkAction(overview, "pool", { pool_id: String(formData.get("pool_id") || "") }, rows);
      return "";
    },
  });
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const poolSelect = dialog?.querySelector("[name='pool_id']");
  const currentHint = dialog?.querySelector("[data-pool-current]");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve pool options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load pools.");
      }
      return response.json();
    })
    .then((data) => {
      if (!poolSelect) {
        return;
      }
      poolSelect.innerHTML = "";
      const noPool = document.createElement("option");
      noPool.value = "";
      noPool.textContent = "No pool";
      poolSelect.appendChild(noPool);
      const pools = Array.isArray(data.pools) ? data.pools : [];
      pools.forEach((pool) => {
        const option = document.createElement("option");
        option.value = pool.id || "";
        option.textContent = pool.label || pool.id || "";
        option.selected = option.value === (data.current_pool || "");
        poolSelect.appendChild(option);
      });
      if (currentHint) {
        if (Array.isArray(data.multiple_memberships) && data.multiple_memberships.length) {
          currentHint.textContent = `Warning: this guest appears in multiple pools (${data.multiple_memberships.join(", ")}). The move will be blocked until that is resolved.`;
          currentHint.hidden = false;
        } else if (data.current_pool) {
          currentHint.textContent = `Current pool: ${data.current_pool}`;
          currentHint.hidden = false;
        }
      }
      poolSelect.disabled = false;
      if (submitButton) {
        submitButton.disabled = Boolean(Array.isArray(data.multiple_memberships) && data.multiple_memberships.length);
      }
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load pools.";
        error.hidden = false;
      }
    });
};

export { openPoolDialog, openTagsDialog };
