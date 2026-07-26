import { createActionDialog, openConfirmDialog } from "./dialogs.js";
import { loadSoftNavigation } from "./navigation.js";
import { escapeHtml, registerPageCleanup } from "./shell.js";

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

// The taskbar carries both endpoints so the approval dialog can be opened from
// the Recent Tasks question on any page, not only from Settings.
const forwarderEndpoints = () => {
  const taskbar = document.querySelector("[data-recent-tasks]");
  return {
    inspectUrl: taskbar?.dataset.logForwarderInspectUrl || "",
    approveUrl: taskbar?.dataset.logForwarderApproveUrl || "",
    csrf: taskbar?.dataset.csrfToken || "",
  };
};

const postForm = async (url, csrf, values) => {
  const response = await fetch(new URL(url, window.location.origin), {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "X-CSRFToken": csrf,
      "X-Requested-With": "fetch",
    },
    body: new URLSearchParams(values),
  });
  const payload = await response.json().catch(() => ({}));
  return { ok: response.ok && payload.ok !== false, payload };
};

// Group the fingerprint so a human can actually compare it against what the
// collector reports. An unbroken 64-character string is checked by nobody.
const groupFingerprint = (value) =>
  String(value || "")
    .toUpperCase()
    .replace(/(.{2})(?=.)/g, "$1:")
    .replace(/((?:..:){15}..):/g, "$1\n");

const trustModeOptions = (certificate) => {
  const issuerNote = certificate.self_signed
    ? "This certificate signed itself, so trusting the issuer means trusting this key to keep issuing for itself."
    : "Renewals issued by the same CA are accepted without asking again.";
  return [
    {
      value: "ca",
      label: "Always trust certificates from this issuer",
      note: certificate.ca_available
        ? `${issuerNote} You are still told if it stops verifying, or is within ${certificate.expiry_warning_days || 7} days of expiry.`
        : "Unavailable: this destination did not send an issuer certificate.",
      disabled: !certificate.ca_available,
      recommended: certificate.ca_available,
    },
    {
      value: "pinned",
      label: "Trust only this exact certificate",
      note: "Every renewal is reported as a change you must approve. Strictest, and the most maintenance.",
      disabled: false,
      recommended: false,
    },
    {
      value: "insecure",
      label: "Accept any certificate (no verification)",
      note: "Encrypted but unauthenticated: anything that answers on this address is accepted. No change or expiry warnings.",
      disabled: false,
      recommended: false,
    },
  ];
};

const certificateSummaryHtml = (certificate, host, port) => {
  const trustedNote = certificate.system_trusted
    ? `<p class="notice notice-success">This certificate already verifies against the installation's trust store. Approving it here is optional.</p>`
    : `<p class="notice notice-warning">This certificate does <strong>not</strong> verify against the installation's trust store${
        certificate.verification_error ? ` (${escapeHtml(certificate.verification_error)})` : ""
      }. It was read over an unverified connection — confirm the fingerprint against the collector itself before trusting it.</p>`;
  const expiryClass = certificate.expires_in_days <= 7 ? " log-forwarder-cert-expiring" : "";
  return `
    <p>Certificate presented by <strong>${escapeHtml(host)}:${escapeHtml(String(port))}</strong>.</p>
    ${trustedNote}
    <dl class="log-forwarder-cert">
      <dt>Subject</dt><dd>${escapeHtml(certificate.subject)}</dd>
      <dt>Issuer</dt><dd>${escapeHtml(certificate.issuer)}${certificate.self_signed ? " <em>(self-signed)</em>" : ""}</dd>
      <dt>Valid</dt><dd class="${expiryClass.trim()}">${escapeHtml(certificate.not_before)} – ${escapeHtml(certificate.not_after)} (${escapeHtml(String(certificate.expires_in_days))} days left)</dd>
      <dt>SHA-256</dt><dd><code class="log-forwarder-fingerprint">${escapeHtml(groupFingerprint(certificate.sha256_fingerprint))}</code></dd>
    </dl>
  `;
};

/**
 * Show what the destination is serving and let a human decide how to trust it.
 *
 * This is SSH's first-contact problem with SSH's answer, and it carries SSH's
 * obligation: the dialog must say plainly that the certificate was read over an
 * unverified connection, because the operator — not the code — is the one
 * verifying it. The three modes are one question with three honest answers, not
 * a recommended path plus two escape hatches; "no verification" is spelled out
 * rather than softened.
 */
const openCertificateTrustDialog = ({ host, port, condition = "", detail = "" }) =>
  new Promise((resolve) => {
    const { inspectUrl, approveUrl, csrf } = forwarderEndpoints();
    if (!inspectUrl || !approveUrl) {
      resolve(null);
      return;
    }

    const dialog = createActionDialog();
    let decided = false;
    const finish = (result) => {
      if (decided) return;
      decided = true;
      resolve(result);
      dialog.close();
    };

    dialog.innerHTML = `
      <div class="vm-action-dialog-form log-forwarder-trust-dialog">
        <div class="vm-action-dialog-heading">
          <h2>Syslog destination certificate</h2>
          <button type="button" data-trust-dismiss aria-label="Close">×</button>
        </div>
        <div class="vm-action-dialog-body" data-trust-body>
          <p data-trust-loading>Reading the certificate from ${escapeHtml(host)}:${escapeHtml(String(port))}…</p>
        </div>
        <p class="form-error" data-trust-error role="alert" hidden></p>
        <div class="form-actions">
          <button class="secondary-action" type="button" data-trust-confirm disabled>Trust this destination</button>
          <button class="secondary-action" type="button" data-trust-cancel>Cancel</button>
        </div>
      </div>
    `;

    const body = dialog.querySelector("[data-trust-body]");
    const errorBox = dialog.querySelector("[data-trust-error]");
    const confirmButton = dialog.querySelector("[data-trust-confirm]");
    const showError = (message) => {
      errorBox.textContent = message;
      errorBox.hidden = false;
    };

    dialog.querySelector("[data-trust-cancel]")?.addEventListener("click", () => finish(null));
    dialog.querySelector("[data-trust-dismiss]")?.addEventListener("click", () => finish(null));
    dialog.addEventListener("close", () => finish(null), { once: true });
    dialog.showModal?.();

    (async () => {
      const { ok, payload } = await postForm(inspectUrl, csrf, { host, port: String(port) });
      if (!ok) {
        body.innerHTML = `<p>${escapeHtml(payload.error || "The destination could not be inspected.")}</p>`;
        confirmButton.remove();
        return;
      }
      const certificate = { ...payload.certificate, expiry_warning_days: payload.expiry_warning_days };
      const conditionNote = condition
        ? `<p class="notice notice-warning">${escapeHtml(detail || "This destination needs your attention.")}</p>`
        : "";
      const currentNote = payload.current
        ? `<p class="log-forwarder-current-trust">Currently approved as <strong>${escapeHtml(payload.current.mode_label)}</strong>${
            payload.current.approved_by ? ` by ${escapeHtml(payload.current.approved_by)}` : ""
          }${payload.current.approved_at ? ` on ${escapeHtml(payload.current.approved_at)}` : ""}.</p>`
        : "";
      const options = trustModeOptions(certificate);
      const defaultOption = options.find((option) => option.recommended) || options[1];
      body.innerHTML = `
        ${conditionNote}
        ${currentNote}
        ${certificateSummaryHtml(certificate, payload.host, payload.port)}
        <fieldset class="log-forwarder-trust-modes">
          <legend>How should this destination be trusted?</legend>
          ${options
            .map(
              (option) => `
            <label class="log-forwarder-trust-mode${option.disabled ? " is-disabled" : ""}">
              <input type="radio" name="trust-mode" value="${escapeHtml(option.value)}"${
                option.disabled ? " disabled" : ""
              }${option.value === defaultOption.value ? " checked" : ""}>
              <span><strong>${escapeHtml(option.label)}</strong><small>${escapeHtml(option.note)}</small></span>
            </label>`
            )
            .join("")}
        </fieldset>
      `;
      confirmButton.disabled = false;
      confirmButton.addEventListener("click", async () => {
        const selected = dialog.querySelector("input[name='trust-mode']:checked");
        if (!selected) {
          showError("Choose how this certificate should be trusted.");
          return;
        }
        confirmButton.disabled = true;
        errorBox.hidden = true;
        const result = await postForm(approveUrl, csrf, {
          host: payload.host,
          port: String(payload.port),
          mode: selected.value,
          // Round-tripped so the server can refuse if the destination started
          // serving something else between showing and approving.
          fingerprint: certificate.sha256_fingerprint,
        });
        if (!result.ok) {
          confirmButton.disabled = false;
          showError(result.payload.error || "The certificate could not be approved.");
          return;
        }
        finish(result.payload.trust);
      });
    })().catch(() => {
      body.innerHTML = "<p>The destination could not be inspected.</p>";
      confirmButton.remove();
    });
  });

// Entry point for the Recent Tasks question badge.
const openLogForwarderCertificateQuestion = async (payload, taskId) => {
  const { dismissTaskQuestion } = await import("./guest-actions.js");
  const approved = await openCertificateTrustDialog({
    host: payload.host || "",
    port: payload.port || 0,
    condition: payload.condition || "",
    detail: payload.detail || "",
  });
  if (approved) {
    // The approval already answered the question server-side; refresh so the
    // badge stops pulsing without a second round trip.
    if (typeof window.pveHelperRefreshRecentTasks === "function") {
      window.pveHelperRefreshRecentTasks();
    }
    return;
  }
  // An expiring certificate has nothing to approve yet, so acknowledging has to
  // be a real answer — otherwise the badge pulses for seven days over a fact the
  // operator has already read and acted on outside this app.
  const acknowledge = await openConfirmDialog({
    title: "Leave this question open?",
    body: "<p>Nothing was approved. Acknowledging records that you have seen this and closes the question; leaving it open keeps it pinned in Recent Tasks.</p>",
    confirmLabel: "Acknowledge",
    cancelLabel: "Leave it open",
    // Both buttons are decisions here, so the × and Esc must not pick one.
    distinguishDismiss: true,
  });
  if (acknowledge === "confirm") {
    await dismissTaskQuestion(taskId, "acknowledged");
  }
};

// Wire the Settings page's Save and Test buttons to the same dialog.
const initLogForwarderTrust = (root = document) => {
  root.querySelectorAll("[data-log-forwarder-trust]").forEach((panel) => {
    if (panel.dataset.trustInitialized === "true") return;
    panel.dataset.trustInitialized = "true";
    panel.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-log-forwarder-approve]");
      if (!button) return;
      event.preventDefault();
      const form = document.querySelector("[data-log-forwarder-form]");
      const host = form?.querySelector("[name='host']")?.value?.trim() || panel.dataset.host || "";
      const port = form?.querySelector("[name='port']")?.value?.trim() || panel.dataset.port || "";
      if (!host) return;
      const approved = await openCertificateTrustDialog({ host, port });
      if (approved) {
        // Soft navigation, not a hard reload: page-local script never re-runs
        // after `innerHTML` replaces the content block, and a full reload here
        // would drop the taskbar's in-flight state for a settings change.
        loadSoftNavigation(window.location.pathname + window.location.search);
      }
    });
  });
};

export {
  initLogForwarderStatus,
  initLogForwarderTrust,
  openCertificateTrustDialog,
  openLogForwarderCertificateQuestion,
};
