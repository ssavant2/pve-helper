import { openConfirmDialog } from "./dialogs.js";
import { loadSoftNavigation } from "./navigation.js";
import { escapeHtml } from "./shell.js";

const CERTIFICATE_SETTINGS_PATH = "/settings/certificates/";

const detailRow = (label, value) =>
  value ? `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>` : "";

const expiryBody = (payload) => {
  const heading = payload.condition === "expired" ? "This certificate has expired." : "This certificate is expiring.";
  return [
    `<p>${escapeHtml(heading)} ${escapeHtml(payload.detail || "")}</p>`,
    '<div class="log-forwarder-status-grid">',
    detailRow("Certificate", payload.certificate_label || ""),
    detailRow("Used as", payload.usage_label || ""),
    detailRow("Subject", payload.subject || ""),
    detailRow("Issuer", payload.issuer || ""),
    detailRow("Expires", (payload.not_after || "").replace("T", " ").slice(0, 16)),
    "</div>",
    "<p>Replacing it is the fix. Acknowledging records that you have seen this and stops the badge; it does not renew anything.</p>",
  ].join("");
};

// A stored certificate cannot be approved the way a remote one can — there is no
// decision to make, only a replacement to upload — so this offers the two answers
// that are actually available and a way to the page that performs the fix.
export const openCertificateExpiryQuestion = async (payload, taskId) => {
  const { dismissTaskQuestion } = await import("./guest-actions.js");
  const answer = await openConfirmDialog({
    title: payload.condition === "expired" ? "Certificate expired" : "Certificate expiring",
    body: expiryBody(payload || {}),
    confirmLabel: "Open Certificates settings",
    cancelLabel: "Acknowledge",
    // Three outcomes, so the × must mean "decide later" rather than silently
    // picking one of the two buttons.
    distinguishDismiss: true,
  });
  if (answer === "confirm") {
    loadSoftNavigation(CERTIFICATE_SETTINGS_PATH);
    return;
  }
  if (answer === "cancel") {
    await dismissTaskQuestion(taskId, "acknowledged");
  }
};

export default { openCertificateExpiryQuestion };
