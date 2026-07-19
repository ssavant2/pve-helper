/**
 * Mount-registration form assistance.
 *
 * The backend identity decides whether a disk in use by another cluster can be
 * presented as an orphan, and byte-equality is what makes that check fire. The
 * datastore's own Proxmox definition already carries the answer for the network
 * backends, so the field is filled from it and only the operator's explicit edit
 * turns it into free text.
 */
export const initStorageAccessForm = (root = document) => {
  const form = root.querySelector("[data-storage-access-form]");
  if (!form || form.dataset.storageAccessReady === "true") return;
  form.dataset.storageAccessReady = "true";

  const datastore = form.querySelector("select[name='cluster_storage']");
  const identity = form.querySelector("input[name='backend_identity']");
  const source = form.querySelector("[data-identity-source]");
  if (!datastore || !identity) return;

  const selected = () => datastore.selectedOptions[0] || null;

  const renderSource = () => {
    if (!source) return;
    const derived = selected()?.dataset.derivedIdentity || "";
    if (!derived) {
      source.textContent = "This backend type does not publish its identity; enter an operator-verified value.";
      source.dataset.state = "manual";
    } else if (identity.value.trim() === derived) {
      source.textContent = `Derived from the Proxmox definition (${derived}).`;
      source.dataset.state = "derived";
    } else {
      source.textContent = `Overridden. The Proxmox definition says ${derived}.`;
      source.dataset.state = "overridden";
    }
  };

  const applyDatastore = () => {
    const derived = selected()?.dataset.derivedIdentity || "";
    const previousDerived = identity.dataset.derivedValue || "";
    // Never clobber an operator's own edit; only replace an untouched field or
    // the value a previously selected datastore derived.
    if (derived && (identity.value.trim() === "" || identity.value.trim() === previousDerived)) {
      identity.value = derived;
    }
    identity.dataset.derivedValue = derived;
    renderSource();
  };

  datastore.addEventListener("change", applyDatastore);
  identity.addEventListener("input", renderSource);
  applyDatastore();
};
