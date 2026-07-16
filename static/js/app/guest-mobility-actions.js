import { guestRowIdentity, openVmFormDialog, submitVmBulkAction } from "./guest-actions.js";
import { createIcons, escapeHtml } from "./shell.js";

const formatMigrateBytes = (bytes) => {
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
};
const openBulkMigrateDialog = (overview, rows) => {
  // Bulk applies one choice to all selected guests. Per-guest NICs/CPU differ,
  // so there's no network mapping or per-guest CPU annotation here — NICs move
  // as-is and each guest's preflight/outcome appears per-row in Recent Tasks.
  // storage = move every guest's disks to the target storage.
  const optionsUrl = rows[0]?.dataset.migrateOptionsUrl || "";
  let optionsData = null;
  const dialog = openVmFormDialog({
    title: "Migrate",
    summary: `${rows.length} selected guests`,
    submitLabel: "Migrate",
    bodyHtml: `
        <p class="form-hint">Applies to all selected guests. NICs move as-is; per-guest results (including any that can't migrate) appear in Recent Tasks.</p>
        <fieldset class="form-field vm-migrate-kind">
          <span>What to migrate</span>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="host" checked> Change host</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="storage"> Change storage (all disks)</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="both"> Change host and storage</label>
        </fieldset>
        <label class="form-field" data-migrate-node-field>
          <span>Target node</span>
          <select name="migrate_target_node" disabled><option value="">Loading nodes…</option></select>
        </label>
        <label class="form-field" data-migrate-storage-field hidden>
          <span>Target storage</span>
          <select name="migrate_target_storage"></select>
        </label>
        <p class="form-hint migrate-warn" data-migrate-warn hidden></p>
      `,
    onSubmit: (formData) => {
      const kind = String(formData.get("migrate_kind") || "");
      const targetNode = String(formData.get("migrate_target_node") || "").trim();
      const targetStorage = String(formData.get("migrate_target_storage") || "").trim();
      if ((kind === "host" || kind === "both") && !targetNode) {
        return "Choose a target node.";
      }
      if ((kind === "storage" || kind === "both") && !targetStorage) {
        return "Choose a target storage.";
      }
      submitVmBulkAction(
        overview,
        "migrate",
        {
          migrate_kind: kind,
          migrate_target_node: kind === "storage" ? "" : targetNode,
          migrate_target_storage: kind === "host" ? "" : targetStorage,
        },
        rows
      );
      return "";
    },
  });
  const nodeField = dialog?.querySelector("[data-migrate-node-field]");
  const storageField = dialog?.querySelector("[data-migrate-storage-field]");
  const nodeSelect = dialog?.querySelector("[name='migrate_target_node']");
  const storageSelect = dialog?.querySelector("[name='migrate_target_storage']");
  const warnBox = dialog?.querySelector("[data-migrate-warn]");
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  let perGuestNics = null;
  const setShown = (field, shown) => {
    if (field) {
      field.style.display = shown ? "" : "none";
    }
  };
  const currentKind = () => dialog?.querySelector("[name='migrate_kind']:checked")?.value || "host";
  // Bulk NICs move as-is, so warn (and name the guests) when a selected guest's
  // bridge isn't realized on the target node — it would land without a network.
  const updateWarnings = () => {
    if (!warnBox) {
      return;
    }
    const kind = currentKind();
    const node = nodeSelect?.value || "";
    if (kind === "storage" || !node || !perGuestNics || !optionsData) {
      warnBox.hidden = true;
      return;
    }
    const available = optionsData.bridges_by_node?.[node] || [];
    const affected = [];
    perGuestNics.forEach((guest) => {
      const missing = [...new Set((guest.bridges || []).filter((bridge) => !available.includes(bridge)))];
      if (missing.length) {
        affected.push(`${guest.label} (${missing.join(", ")})`);
      }
    });
    if (affected.length) {
      warnBox.textContent = `⚠ Bridges not on ${node} — these guests will land without a network: ${affected.join("; ")}.`;
      warnBox.hidden = false;
    } else {
      warnBox.hidden = true;
    }
  };
  const fillStorage = (nodeName) => {
    if (!storageSelect) {
      return;
    }
    const ids = optionsData?.storages_by_node?.[nodeName] || [];
    storageSelect.innerHTML = "";
    if (!ids.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No compatible storage";
      storageSelect.appendChild(option);
    } else {
      ids.forEach((id) => {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = id;
        storageSelect.appendChild(option);
      });
    }
  };
  const syncFields = () => {
    const kind = currentKind();
    setShown(nodeField, kind !== "storage");
    setShown(storageField, kind !== "host");
    // storage: each guest moves on its own node — offer the source-node storages
    // (rows[0]'s node); both: offer the target node's storages.
    if (kind === "storage") {
      fillStorage(optionsData?.current_node || "");
    } else if (kind === "both") {
      fillStorage(nodeSelect?.value || "");
    }
    updateWarnings();
  };
  dialog?.querySelectorAll("[name='migrate_kind']").forEach((radio) => {
    radio.addEventListener("change", syncFields);
  });
  nodeSelect?.addEventListener("change", syncFields);
  // Fetch each selected guest's NIC bridges once for the missing-bridge warning.
  const nicsUrl = overview?.dataset.vmMigrateNicsUrl || "";
  if (nicsUrl) {
    const csrf = overview.querySelector("[data-vm-bulk-form] [name=csrfmiddlewaretoken]")?.value || "";
    const body = new URLSearchParams();
    rows.forEach((row) => {
      body.append("guest", row.dataset.guestTarget || "");
    });
    fetch(new URL(nicsUrl, window.location.origin), {
      method: "POST",
      headers: { "X-CSRFToken": csrf, "X-Requested-With": "fetch" },
      body,
    })
      .then((response) => (response.ok ? response.json() : null))
      .then((data) => {
        perGuestNics = Array.isArray(data?.guests) ? data.guests : [];
        updateWarnings();
      })
      .catch(() => {
        /* the NIC warning is best-effort */
      });
  }
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve migrate options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load migrate options.");
      }
      return response.json();
    })
    .then((data) => {
      optionsData = data;
      if (nodeSelect) {
        const online = (Array.isArray(data.nodes) ? data.nodes : []).filter((node) => node.online);
        nodeSelect.innerHTML = "";
        if (!online.length) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "No other online cluster node";
          nodeSelect.appendChild(option);
          nodeSelect.disabled = true;
        } else {
          online.forEach((node) => {
            const option = document.createElement("option");
            option.value = node.node;
            option.textContent = node.node;
            nodeSelect.appendChild(option);
          });
          nodeSelect.disabled = false;
        }
      }
      if (submitButton) {
        submitButton.disabled = false;
      }
      syncFields();
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load migrate options.";
        error.hidden = false;
      }
    });
};

const openMigrateDialog = (overview, rows) => {
  if (rows.length > 1) {
    openBulkMigrateDialog(overview, rows);
    return;
  }
  const row = rows[0];
  const label = row?.dataset.guestLabel || "guest";
  const optionsUrl = row?.dataset.migrateOptionsUrl || "";
  let optionsData = null;
  const dialog = openVmFormDialog({
    title: "Migrate",
    summary: label,
    submitLabel: "Migrate",
    bodyHtml: `
        <fieldset class="form-field vm-migrate-kind">
          <span>What to migrate</span>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="host" checked> Change host</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="storage"> Change storage</label>
          <label class="form-field-inline"><input type="radio" name="migrate_kind" value="both"> Change host and storage</label>
        </fieldset>
        <label class="form-field" data-migrate-node-field>
          <span>Target node</span>
          <select name="migrate_target_node" disabled><option value="">Loading nodes…</option></select>
        </label>
        <label class="form-field" data-migrate-storage-field hidden>
          <span>Target storage</span>
          <select name="migrate_target_storage"></select>
          <span class="form-hint" data-migrate-storage-hint hidden>All of the guest's disks move to this storage.</span>
        </label>
        <div class="form-field migrate-net-check" data-migrate-net-field hidden>
          <span>Network mapping</span>
          <label class="form-field-inline"><input type="checkbox" data-migrate-net-ignore> Ignore network mapping (migrate NICs as-is)</label>
          <div data-migrate-net-body></div>
        </div>
        <p class="form-hint" data-migrate-hint hidden></p>
        <p class="form-hint migrate-warn" data-migrate-warn hidden></p>
      `,
    onSubmit: (formData) => {
      const kind = String(formData.get("migrate_kind") || "");
      const targetNode = String(formData.get("migrate_target_node") || "").trim();
      const targetStorage = String(formData.get("migrate_target_storage") || "").trim();
      if (kind === "host" || kind === "both") {
        if (!targetNode) {
          return "Choose a target node.";
        }
        const opt = nodeSelect?.selectedOptions?.[0];
        if (opt?.dataset.allowed === "false") {
          return opt.dataset.reason
            ? `That node can't be a migration target: ${opt.dataset.reason}.`
            : "That node can't be a migration target.";
        }
        if (opt?.dataset.cpuOk === "false") {
          return opt.dataset.cpuReason ? `${opt.dataset.cpuReason}.` : "The target host can't run this VM's CPU model.";
        }
      }
      if ((kind === "both" || kind === "storage") && !targetStorage) {
        return "Choose a target storage.";
      }
      const fields = {
        migrate_kind: kind,
        migrate_target_node: kind === "storage" ? "" : targetNode,
        migrate_target_storage: kind === "host" ? "" : targetStorage,
      };
      if (kind !== "storage") {
        const remap = collectRemap();
        if (Object.keys(remap).length) {
          fields.migrate_net_remap = JSON.stringify(remap);
        }
      }
      submitVmBulkAction(overview, "migrate", fields, rows);
      return "";
    },
  });
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  const nodeField = dialog?.querySelector("[data-migrate-node-field]");
  const storageField = dialog?.querySelector("[data-migrate-storage-field]");
  const storageHint = dialog?.querySelector("[data-migrate-storage-hint]");
  const nodeSelect = dialog?.querySelector("[name='migrate_target_node']");
  const storageSelect = dialog?.querySelector("[name='migrate_target_storage']");
  const netField = dialog?.querySelector("[data-migrate-net-field]");
  const netBody = dialog?.querySelector("[data-migrate-net-body]");
  const netIgnore = dialog?.querySelector("[data-migrate-net-ignore]");
  const hint = dialog?.querySelector("[data-migrate-hint]");
  const warnBox = dialog?.querySelector("[data-migrate-warn]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  const currentKind = () => dialog?.querySelector("[name='migrate_kind']:checked")?.value || "host";
  const fillStorage = (nodeName) => {
    if (!storageSelect) {
      return;
    }
    const ids = optionsData?.storages_by_node?.[nodeName] || [];
    storageSelect.innerHTML = "";
    if (!ids.length) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = "No compatible storage";
      storageSelect.appendChild(option);
    } else {
      ids.forEach((id) => {
        const option = document.createElement("option");
        option.value = id;
        option.textContent = id;
        storageSelect.appendChild(option);
      });
    }
  };
  const updateHint = () => {
    if (!hint) {
      return;
    }
    const kind = currentKind();
    const opt = nodeSelect?.selectedOptions?.[0];
    if ((kind === "host" || kind === "both") && opt?.dataset.allowed === "false") {
      hint.textContent = opt.dataset.reason
        ? `Blocked target: ${opt.dataset.reason}.`
        : "This node can't be a migration target.";
      hint.hidden = false;
      return;
    }
    if ((kind === "host" || kind === "both") && optionsData?.running) {
      hint.textContent =
        optionsData.object_type === "vm"
          ? "Running VM → live (online) migration."
          : "Running container → restart migration (brief downtime).";
      hint.hidden = false;
      return;
    }
    hint.hidden = true;
  };
  // Toggle via inline display: the dialog's .form-field is `display: grid`,
  // which would override the [hidden] attribute and leave fields visible.
  const setShown = (field, shown) => {
    if (field) {
      field.style.display = shown ? "" : "none";
    }
  };
  // Only send a remap for NICs whose chosen bridge differs from the current
  // one; skip entirely when "ignore network mapping" is ticked.
  const collectRemap = () => {
    const out = {};
    if (netIgnore?.checked) {
      return out;
    }
    netBody?.querySelectorAll("[data-remap-net]").forEach((select) => {
      const value = String(select.value || "").trim();
      if (value && value !== select.dataset.current) {
        out[select.dataset.remapNet] = value;
      }
    });
    return out;
  };
  // Proxmox has no per-host port groups: a NIC's bridge name must exist on the
  // target node. Only surface a NIC when there's a decision to make — its
  // bridge is missing on the target, or it's on a default `vmbrN` bridge (which
  // exists everywhere, so picking a specific net is worthwhile). A custom
  // bridge already present on the target (e.g. server10→server10) needs no
  // prompt. Choosing a different bridge edits the guest config (permanent,
  // cluster-wide — see the backend).
  const isDefaultBridge = (bridge) => /^vmbr\d+$/.test(bridge || "");
  const renderNetCheck = () => {
    if (!netField || !netBody) {
      return;
    }
    const kind = currentKind();
    const node = nodeSelect?.value || "";
    const nics = Array.isArray(optionsData?.guest_nics) ? optionsData.guest_nics : [];
    const available = optionsData?.bridges_by_node?.[node] || [];
    const relevant =
      kind === "storage" || !node
        ? []
        : nics.filter((nic) => !available.includes(nic.bridge) || isDefaultBridge(nic.bridge));
    if (!relevant.length) {
      setShown(netField, false);
      netBody.innerHTML = "";
      return;
    }
    setShown(netField, true);
    if (netIgnore?.checked) {
      netBody.innerHTML = "";
      return;
    }
    // Selectable = bridges realized on the node + all cluster SDN vnets (which
    // are cluster-scoped). "present" (for the warning/default) stays realized-only.
    const vnets = Array.isArray(optionsData?.sdn_vnets) ? optionsData.sdn_vnets : [];
    const selectable = Array.from(new Set([...available, ...vnets])).sort();
    netBody.innerHTML = relevant
      .map((nic) => {
        const present = available.includes(nic.bridge);
        const keepOption = present
          ? `<option value="${escapeHtml(nic.bridge)}" selected>${escapeHtml(nic.bridge)} (unchanged)</option>`
          : `<option value="" selected>Keep “${escapeHtml(nic.bridge)}” — missing on ${escapeHtml(node)}</option>`;
        const options = keepOption.concat(
          selectable
            .filter((bridge) => bridge !== nic.bridge)
            .map((bridge) => `<option value="${escapeHtml(bridge)}">${escapeHtml(bridge)}</option>`)
            .join("")
        );
        const warn = present
          ? ""
          : `<span class="form-hint">⚠ bridge “${escapeHtml(nic.bridge)}” is not on ${escapeHtml(node)} — the NIC will have no network unless remapped.</span>`;
        return `<div class="migrate-net-warn">
              <label class="form-field-inline"><span>${escapeHtml(nic.key)}</span><select data-remap-net="${escapeHtml(nic.key)}" data-current="${escapeHtml(nic.bridge)}">${options}</select></label>
              ${warn}
            </div>`;
      })
      .join("");
    setShown(netField, true);
  };
  netIgnore?.addEventListener("change", renderNetCheck);
  // Preflight cautions Proxmox itself doesn't check: passthrough/local
  // resources that block migration, cpu=host (non-portable for live
  // migration), and a target host whose CPU can't run the VM's model (EVC-lite).
  const updateWarnings = () => {
    if (!warnBox) {
      return;
    }
    const kind = currentKind();
    const lines = [];
    if (kind !== "storage") {
      const local = Array.isArray(optionsData?.local_resources) ? optionsData.local_resources : [];
      if (local.length) {
        lines.push(`⚠ Local/passthrough resources may block migration: ${local.join(", ")}.`);
      }
      const opt = nodeSelect?.selectedOptions?.[0];
      if (opt?.dataset.cpuOk === "false") {
        lines.push(`⚠ ${opt.dataset.cpuReason || "The target host can't run this VM's CPU model."}.`);
      }
      // cpu=host is only risky for a live migration between differing hosts;
      // silent when the guest is stopped (offline) or the CPUs match.
      if ((optionsData?.guest_cpu || "") === "host" && optionsData?.running && opt?.dataset.hostCpuMatch === "false") {
        lines.push(
          `⚠ cpu=host and ${opt.dataset.hostCpuReason || "the target host CPU differs"} — live migration will likely crash the guest (pin a CPU model, or migrate while stopped).`
        );
      }
    }
    // Target-storage capacity: warn when free space looks short of the guest's
    // provisioned disks (actual usage may be less for thin/sparse volumes).
    if (kind === "storage" || kind === "both") {
      const storageNode = kind === "storage" ? optionsData?.current_node || "" : nodeSelect?.value || "";
      const storageId = storageSelect?.value || "";
      const need = Number(optionsData?.guest_disk_bytes || 0);
      const free = optionsData?.storage_free_by_node?.[storageNode]?.[storageId];
      if (need > 0 && typeof free === "number" && free < need) {
        lines.push(
          `⚠ Target storage ${storageId} has ${formatMigrateBytes(free)} free, but the guest's disks are provisioned at ${formatMigrateBytes(need)} (thin/sparse disks may still fit).`
        );
      }
    }
    if (lines.length) {
      warnBox.innerHTML = lines.map((line) => escapeHtml(line)).join("<br>");
      warnBox.hidden = false;
    } else {
      warnBox.hidden = true;
    }
  };
  const syncFields = () => {
    const kind = currentKind();
    setShown(nodeField, kind !== "storage");
    setShown(storageField, kind !== "host");
    if (storageHint) {
      storageHint.hidden = kind !== "storage";
    }
    if (kind === "storage") {
      fillStorage(optionsData?.current_node || "");
    } else if (kind === "both") {
      fillStorage(nodeSelect?.value || "");
    }
    updateHint();
    renderNetCheck();
    updateWarnings();
  };
  dialog?.querySelectorAll("[name='migrate_kind']").forEach((radio) => {
    radio.addEventListener("change", syncFields);
  });
  nodeSelect?.addEventListener("change", syncFields);
  // Storage choice only affects the capacity warning — don't re-run syncFields
  // (which would repopulate and reset the select).
  storageSelect?.addEventListener("change", updateWarnings);
  syncFields();
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve migrate options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load migrate options.");
      }
      return response.json();
    })
    .then((data) => {
      optionsData = data;
      if (nodeSelect) {
        nodeSelect.innerHTML = "";
        const nodes = Array.isArray(data.nodes) ? data.nodes : [];
        if (!nodes.length) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "No other cluster node";
          nodeSelect.appendChild(option);
          nodeSelect.disabled = true;
          // Only storage-only migration is possible without a second node.
          dialog?.querySelectorAll("[name='migrate_kind']").forEach((radio) => {
            if (radio.value !== "storage") {
              radio.disabled = true;
            }
          });
          const storageRadio = dialog?.querySelector("[name='migrate_kind'][value='storage']");
          if (storageRadio) {
            storageRadio.checked = true;
          }
        } else {
          nodes.forEach((node) => {
            const option = document.createElement("option");
            option.value = node.node;
            const blockReason = !node.allowed
              ? node.reason || "blocked"
              : node.cpu_ok === false
                ? node.cpu_reason || "CPU incompatible"
                : "";
            option.textContent = blockReason ? `${node.node} — ${blockReason}` : node.node;
            option.dataset.allowed = node.allowed ? "true" : "false";
            option.dataset.reason = node.reason || "";
            option.dataset.cpuOk = node.cpu_ok === false ? "false" : "true";
            option.dataset.cpuReason = node.cpu_reason || "";
            option.dataset.hostCpuMatch = node.host_cpu_match === false ? "false" : "true";
            option.dataset.hostCpuReason = node.host_cpu_reason || "";
            nodeSelect.appendChild(option);
          });
          const firstAllowed =
            nodes.find((node) => node.allowed && node.cpu_ok !== false) || nodes.find((node) => node.allowed);
          nodeSelect.value = firstAllowed ? firstAllowed.node : nodes[0].node;
          nodeSelect.disabled = false;
        }
      }
      if (submitButton) {
        submitButton.disabled = false;
      }
      syncFields();
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load migrate options.";
        error.hidden = false;
      }
    });
};

const openCloneDialog = (overview, rows, { toTemplate = false } = {}) => {
  const row = rows[0];
  const label = row?.dataset.guestLabel || "guest";
  const guestName = row?.dataset.guestName || "";
  let usedVmids = new Set();
  const dialog = openVmFormDialog({
    title: toTemplate
      ? "Clone to Template"
      : row?.dataset.guestTemplate === "true"
        ? "New VM from This Template"
        : "Clone",
    summary: label,
    submitLabel: toTemplate ? "Clone to Template" : "Clone",
    bodyHtml: `
        <label class="form-field">
          <span>New VMID</span>
          <input type="number" name="clone_newid" min="1" step="1" required disabled>
        </label>
        <label class="form-field">
          <span>Name</span>
          <input type="text" name="clone_name" autocomplete="off" required value="${escapeHtml(guestName ? `${guestName}-clone` : "")}">
        </label>
        <label class="form-field">
          <span>Storage</span>
          <select name="clone_storage" disabled>
            <option value="">Loading storages...</option>
          </select>
        </label>
        ${
          toTemplate
            ? '<p class="form-hint">Creates a full clone and converts the new VM to a template after the clone completes.</p>'
            : `<label class="form-field form-field-inline">
          <input type="checkbox" name="clone_full" value="1" checked>
          <span>Full clone</span>
        </label>`
        }
        <p class="form-hint" data-clone-full-hint hidden></p>
      `,
    onSubmit: (formData) => {
      const newid = String(formData.get("clone_newid") || "").trim();
      if (!/^[0-9]+$/.test(newid) || Number(newid) <= 0) {
        return "New VMID must be a positive whole number.";
      }
      if (usedVmids.has(Number(newid))) {
        return `VMID ${newid} is already in use — pick a free ID.`;
      }
      const name = String(formData.get("clone_name") || "").trim();
      if (!name) {
        return "Name is required.";
      }
      const fields = {
        clone_newid: newid,
        clone_name: name,
        clone_storage: String(formData.get("clone_storage") || "").trim(),
        clone_full: fullCheckbox?.checked ? "1" : "0",
      };
      if (toTemplate) {
        const { type, vmid } = guestRowIdentity(row);
        const url = `/vms/${encodeURIComponent(type)}/${encodeURIComponent(vmid)}/clone-to-template/`;
        submitVmBulkAction(overview, "clone_to_template", fields, rows, url);
      } else {
        submitVmBulkAction(overview, "clone", fields, rows);
      }
      return "";
    },
  });
  const submitButton = dialog?.querySelector("[data-vm-dialog-submit]");
  const idInput = dialog?.querySelector("[name='clone_newid']");
  const storageSelect = dialog?.querySelector("[name='clone_storage']");
  const fullCheckbox = dialog?.querySelector("[name='clone_full']");
  const error = dialog?.querySelector("[data-vm-dialog-error]");
  if (submitButton) {
    submitButton.disabled = true;
  }
  const syncStorageState = () => {
    if (storageSelect) {
      storageSelect.disabled =
        toTemplate || Boolean(fullCheckbox && !fullCheckbox.checked) || storageSelect.options.length === 0;
    }
  };
  fullCheckbox?.addEventListener("change", syncStorageState);
  const optionsUrl = row?.dataset.cloneOptionsUrl || "";
  if (!optionsUrl) {
    if (error) {
      error.textContent = "Could not resolve clone options URL.";
      error.hidden = false;
    }
    return;
  }
  fetch(new URL(optionsUrl, window.location.origin), { headers: { Accept: "application/json" } })
    .then((response) => {
      if (!response.ok) {
        throw new Error("Could not load clone options.");
      }
      return response.json();
    })
    .then((data) => {
      usedVmids = new Set((Array.isArray(data.used_vmids) ? data.used_vmids : []).map(Number));
      if (idInput) {
        idInput.value = data.nextid || "";
        idInput.disabled = false;
        // Live feedback so a taken ID is caught before submit, not after.
        const validateId = () => {
          const value = Number(idInput.value);
          const taken = Number.isFinite(value) && value > 0 && usedVmids.has(value);
          idInput.setCustomValidity(taken ? "VMID already in use" : "");
          if (error) {
            if (taken) {
              error.textContent = `VMID ${idInput.value} is already in use — pick a free ID.`;
              error.hidden = false;
            } else if (error.textContent.includes("already in use")) {
              error.hidden = true;
            }
          }
        };
        idInput.addEventListener("input", validateId);
      }
      if (storageSelect) {
        storageSelect.innerHTML = "";
        const storages = Array.isArray(data.storages) ? data.storages : [];
        if (storages.length === 0) {
          const option = document.createElement("option");
          option.value = "";
          option.textContent = "Same/default storage";
          storageSelect.appendChild(option);
        } else {
          storages.forEach((storage) => {
            const option = document.createElement("option");
            option.value = storage.id || "";
            option.textContent = storage.label || storage.id || "";
            if (storage.id === data.default_storage) {
              option.selected = true;
            }
            storageSelect.appendChild(option);
          });
        }
      }
      const nameInput = dialog?.querySelector("[name='clone_name']");
      if (nameInput && !nameInput.value && data.suggested_name) {
        nameInput.value = data.suggested_name;
      }
      // Linked clones are only valid from a template; force a full clone for a
      // regular guest so Proxmox does not reject it.
      const fullHint = dialog?.querySelector("[data-clone-full-hint]");
      if (fullCheckbox && !data.is_template) {
        fullCheckbox.checked = true;
        fullCheckbox.disabled = true;
        if (fullHint) {
          fullHint.textContent = "Linked clones require a template — this guest will be full-cloned.";
          fullHint.hidden = false;
        }
      }
      syncStorageState();
      if (submitButton) {
        submitButton.disabled = false;
      }
    })
    .catch((errorObject) => {
      if (error) {
        error.textContent = errorObject.message || "Could not load clone options.";
        error.hidden = false;
      }
    });
};

const openDestroyDialog = (overview, rows) => {
  const row = rows[0];
  const label = row?.dataset.guestLabel || "guest";
  const target = row?.dataset.guestTarget || "";
  // target is like "vm:986@pve3" — confirm on the numeric VMID only.
  const vmid = (target.split(":")[1] || "").split("@")[0];
  openVmFormDialog({
    title: "Remove VM/CT",
    summary: label,
    submitLabel: "Remove",
    submitClass: "primary-action danger-action",
    bodyHtml: `
        <div class="danger-confirmation">
          <i data-lucide="triangle-alert" aria-hidden="true"></i>
          <div>
            <p>Destroy this guest and remove it from Proxmox inventory.</p>
            <p class="warning-copy">Referenced disks will be destroyed by Proxmox.</p>
          </div>
        </div>
        <label class="form-field">
          <span>Enter VMID to confirm (${escapeHtml(vmid)})</span>
          <input type="number" name="destroy_confirm_vmid" min="1" step="1" autocomplete="off" required>
        </label>
        <label class="form-field form-field-inline">
          <input type="checkbox" name="destroy_purge" value="1" checked>
          <span>Purge from job configurations</span>
        </label>
        <label class="form-field form-field-inline">
          <input type="checkbox" name="destroy_unreferenced_disks" value="1" checked>
          <span>Destroy unreferenced disks owned by guest</span>
        </label>
      `,
    onSubmit: (formData) => {
      const confirmed = String(formData.get("destroy_confirm_vmid") || "").trim();
      if (confirmed !== vmid) {
        return `Enter ${vmid} to confirm.`;
      }
      submitVmBulkAction(
        overview,
        "destroy",
        {
          destroy_confirm_vmid: confirmed,
          destroy_purge: formData.get("destroy_purge") === "1" ? "1" : "0",
          destroy_unreferenced_disks: formData.get("destroy_unreferenced_disks") === "1" ? "1" : "0",
        },
        rows
      );
      return "";
    },
  });
  createIcons();
};

const openUnTemplateDialog = (overview, rows) => {
  const row = rows[0];
  const label = row?.dataset.guestLabel || row?.dataset.guestName || "Template";
  const target = row?.dataset.guestTarget || "";
  const vmid = (target.split(":")[1] || "").split("@")[0];
  openVmFormDialog({
    title: "Convert Template Back to VM",
    summary: label,
    submitLabel: "Convert to VM",
    submitClass: "primary-action danger-action",
    bodyHtml: `
        <div class="danger-confirmation">
          <i data-lucide="triangle-alert" aria-hidden="true"></i>
          <div>
            <p>This clears the Proxmox template flag and makes the original guest a VM again.</p>
            <p class="warning-copy">Proxmox does not provide an officially supported reverse for this operation. Linked clones, snapshots, locked templates, protected templates, and unsupported storage are blocked.</p>
          </div>
        </div>
        <label class="form-field form-field-inline">
          <input type="checkbox" name="untemplate_acknowledge" value="convert" required>
          <span>I understand that this changes the template back into its original VM.</span>
        </label>
        <label class="form-field">
          <span>Enter VMID to confirm (${escapeHtml(vmid)})</span>
          <input type="number" name="untemplate_confirm_vmid" min="1" step="1" autocomplete="off" required>
        </label>
      `,
    onSubmit: (formData) => {
      const confirmed = String(formData.get("untemplate_confirm_vmid") || "").trim();
      if (confirmed !== vmid) {
        return `Enter ${vmid} to confirm.`;
      }
      if (formData.get("untemplate_acknowledge") !== "convert") {
        return "Confirm that you understand this operation.";
      }
      submitVmBulkAction(
        overview,
        "untemplate",
        {
          untemplate_confirm_vmid: confirmed,
          untemplate_acknowledge: "convert",
        },
        rows
      );
      return "";
    },
  });
  createIcons();
};

export {
  formatMigrateBytes,
  openBulkMigrateDialog,
  openCloneDialog,
  openDestroyDialog,
  openMigrateDialog,
  openUnTemplateDialog,
};
