import { openConfirmDialog, openFieldsDialog, openNoticeDialog } from "./dialogs.js";
import { escapeHtml } from "./shell.js";

const countRow = (label, value) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(String(value ?? 0))}</dd>`;

const impactBody = (payload) => {
  const cluster = payload.cluster || {};
  const impact = payload.impact || {};
  const counts = impact.counts || {};
  const verification =
    impact.identity_verification === "matched"
      ? `Matched through ${impact.endpoint || "the selected endpoint"}`
      : "Skipped — forced retirement makes no provider request";
  const consumerRows = (impact.storage_consumers || [])
    .map(
      (consumer) => `
        <li>
          <a href="${escapeHtml(consumer.url || "#")}">${escapeHtml(consumer.storage_name || consumer.storage_id)}</a>
          on ${escapeHtml(consumer.node || "unknown node")}
        </li>
      `
    )
    .join("");
  const blockers = (impact.blockers || [])
    .map((blocker) => `<li data-retirement-blocker="${escapeHtml(blocker.code)}">${escapeHtml(blocker.message)}</li>`)
    .join("");

  return `
    <p>
      <strong>${escapeHtml(cluster.display_name || "")}</strong>
      (<code>${escapeHtml(cluster.key || "")}</code>)
    </p>
    <dl class="cluster-retirement-impact">
      ${countRow("Identity verification", verification)}
      ${countRow("Schedules", counts.schedules)}
      ${countRow("Not-started schedule runs", counts.schedule_runs_not_started)}
      ${countRow("Active schedule runs", counts.schedule_runs_active)}
      ${countRow("Current guest projections", counts.current_projections)}
      ${countRow("Inventory history rows", counts.history)}
      ${countRow("Storage definitions", counts.storage_definitions)}
      ${countRow("Storage consumers", counts.storage_consumers)}
      ${countRow("Pending / active consoles", `${counts.consoles_pending || 0} / ${counts.consoles_active || 0}`)}
      ${countRow(
        "Queued / running provider operations",
        `${counts.provider_operations_queued || 0} / ${counts.provider_operations_running || 0}`
      )}
      ${countRow("Active installation scans", counts.active_scans)}
    </dl>
    ${
      consumerRows
        ? `<h3>Storage consumers to resolve</h3><ul class="cluster-retirement-consumers">${consumerRows}</ul>`
        : ""
    }
    ${blockers ? `<h3>Retirement is blocked</h3><ul class="cluster-retirement-blockers">${blockers}</ul>` : ""}
    <h3>Preserved</h3>
    <p>The cluster tombstone, permanent key, inventory history and Audit evidence remain.</p>
    <h3>Removed or deactivated</h3>
    <p>
      Endpoints, transport trust, pve-helper's encrypted credential copy, schedules,
      current projections and current storage publication are removed or retired.
    </p>
    <p>
      The API token still exists in Proxmox. Revoke it there separately whenever the
      site is reachable.
    </p>
  `;
};

const errorMessage = (payload, fallback) => payload?.error?.message || fallback;

const postRetirement = async (form, body) => {
  // `input[name="action"]` is exposed as a named property on HTMLFormElement
  // and shadows `form.action`; read the URL attribute explicitly.
  const response = await fetch(new URL(form.getAttribute("action") || window.location.href, window.location.href), {
    method: "POST",
    body,
    headers: { Accept: "application/json", "X-Requested-With": "fetch" },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload.ok) {
    throw new Error(errorMessage(payload, `Retirement request failed: HTTP ${response.status}.`));
  }
  return payload;
};

const showFailure = (message) =>
  openNoticeDialog({
    title: "Cluster retirement refused",
    body: `<p>${escapeHtml(message)}</p>`,
  });

const runRetirement = async (form) => {
  const button = form.querySelector('button[type="submit"]');
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const preflightBody = new FormData(form);
    const payload = await postRetirement(form, preflightBody);
    const body = impactBody(payload);
    if (!payload.ready || !payload.confirmation) {
      await openNoticeDialog({
        title: "Cluster retirement is blocked",
        body,
      });
      return;
    }

    const mode = form.dataset.retirementMode || "";
    const clusterKey = payload.cluster?.key || form.dataset.clusterKey || "";
    const reasonMaxLength = Number.parseInt(form.dataset.reasonMaxLength || "1000", 10);
    const fields =
      mode === "forced"
        ? [
            {
              name: "typed_cluster_key",
              label: "Type the permanent cluster key",
              required: true,
              validate: (value) => (value === clusterKey ? "" : `Type ${clusterKey} exactly.`),
            },
            {
              name: "reason",
              label: "Why is this site permanently unavailable?",
              type: "textarea",
              rows: 4,
              required: true,
              maxLength: reasonMaxLength,
              hint: "This reason is retained in Audit evidence.",
            },
          ]
        : [];
    const first = await openFieldsDialog({
      title: mode === "forced" ? "Force-retire cluster" : "Retire cluster",
      body,
      fields,
      confirmLabel: mode === "forced" ? "Site is permanently unavailable" : "Continue",
      cancelLabel: mode === "forced" ? "The site may return" : "Cancel",
      danger: true,
      distinguishDismiss: mode === "forced",
    });
    const values = mode === "forced" ? first?.values : first;
    if ((mode === "forced" && first?.outcome !== "confirm") || (mode !== "forced" && !values)) {
      return;
    }

    const finalConfirmed = await openConfirmDialog({
      title: "Are you really sure?",
      body: `
        <p>
          Retirement is irreversible. <strong>${escapeHtml(payload.cluster?.display_name || "")}</strong>
          will no longer be a managed Proxmox connection.
        </p>
        <p>The permanent key <code>${escapeHtml(clusterKey)}</code> remains reserved.</p>
      `,
      confirmLabel: mode === "forced" ? "Force-retire permanently" : "Retire permanently",
      cancelLabel: "Go back",
      danger: true,
      swapActions: true,
    });
    if (!finalConfirmed) return;

    const finalBody = new FormData(form);
    finalBody.set("action", "retire");
    finalBody.set("confirmation", payload.confirmation);
    finalBody.set("typed_cluster_key", values?.typed_cluster_key || "");
    finalBody.set("reason", values?.reason || "");
    finalBody.set("permanent_unavailability_asserted", mode === "forced" ? "yes" : "");
    const result = await postRetirement(form, finalBody);
    window.location.assign(result.redirect_url || "/clusters/");
  } catch (error) {
    await showFailure(error instanceof Error ? error.message : "Cluster retirement failed safely.");
  } finally {
    if (button) button.disabled = false;
  }
};

const initClusterRetirement = (root = document) => {
  root.querySelectorAll("[data-cluster-retirement-form]").forEach((form) => {
    if (form.dataset.initialized === "true") return;
    form.dataset.initialized = "true";
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      event.stopPropagation();
      runRetirement(form);
    });
  });
};

export { initClusterRetirement };
