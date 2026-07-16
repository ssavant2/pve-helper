const initHardwareEditor = (root = document) => {
  const page = root.querySelector ? root.querySelector(".hardware-editor-page") : null;
  if (!page || page.dataset.hwInit === "true") {
    return;
  }
  page.dataset.hwInit = "true";
  const form = page.querySelector(".hardware-editor-form");

  const closeKebabs = (except) => {
    page.querySelectorAll(".hw-kebab-menu").forEach((menu) => {
      if (menu !== except) {
        menu.hidden = true;
      }
    });
  };

  const closeAddMenu = (event) => {
    const details = page.querySelector("[data-hw-add]");
    if (details && !event?.target.closest("[data-hw-add]")) {
      details.open = false;
    }
  };

  const syncHotplug = (editor) => {
    const value = editor.querySelector("[data-hotplug-value]");
    if (!value) {
      return;
    }
    value.value = Array.from(editor.querySelectorAll("[data-hotplug-token]"))
      .filter((checkbox) => checkbox.checked)
      .map((checkbox) => checkbox.dataset.hotplugToken)
      .join(",");
  };

  const syncBootOrder = (editor) => {
    const value = editor.querySelector("[data-boot-order-value]");
    if (!value) {
      return;
    }
    const enabled = Array.from(editor.querySelectorAll("[data-boot-device]"))
      .filter((row) => {
        const checkbox = row.querySelector("[data-boot-enabled]");
        return checkbox?.checked;
      })
      .map((row) => row.dataset.bootDevice)
      .filter(Boolean);
    value.value = enabled.length ? `order=${enabled.join(";")}` : "";
  };

  const resizeTextarea = (textarea) => {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.max(textarea.scrollHeight, 76)}px`;
  };

  const initAutogrowTextarea = (textarea) => {
    if (textarea.dataset.autogrowInitialized === "true") {
      return;
    }
    textarea.dataset.autogrowInitialized = "true";
    resizeTextarea(textarea);
    textarea.addEventListener("input", () => resizeTextarea(textarea));
  };

  const activateDevice = (type, addBtn) => {
    if (type === "cdrom") {
      const cd = page.querySelector("#device-cdrom");
      if (cd) {
        cd.classList.add("is-open");
        const toggle = cd.querySelector("[data-hw-toggle]");
        if (toggle) {
          toggle.setAttribute("aria-expanded", "true");
        }
        cd.scrollIntoView({ behavior: "smooth", block: "center" });
      }
      return;
    }
    const template = page.querySelector(`[data-new-device="${type}"][data-new-template="true"]`);
    if (!template) {
      return;
    }
    if (
      addBtn?.hasAttribute("data-add-singleton") &&
      page.querySelector(`[data-new-device="${type}"]:not([hidden]):not([data-new-template="true"])`)
    ) {
      addBtn.disabled = true;
      return;
    }
    const item = template.cloneNode(true);
    item.dataset.newTemplate = "false";
    item.hidden = false;
    template.parentElement.insertBefore(item, template);
    item.hidden = false;
    item.classList.add("is-open");
    item.querySelectorAll("[data-new-input]").forEach((el) => {
      el.disabled = false;
    });
    item.querySelectorAll("[data-new-trigger]").forEach((el) => {
      el.checked = true;
    });
    if (addBtn?.hasAttribute("data-add-singleton")) {
      addBtn.disabled = true;
    }
    const first = item.querySelector("[data-new-required], [data-new-input]:not([hidden])");
    if (first) {
      first.focus();
    }
    item.scrollIntoView({ behavior: "smooth", block: "center" });
  };

  const deactivateNew = (item) => {
    const type = item.dataset.newDevice;
    if (item.dataset.newTemplate === "true") {
      return;
    }
    item.remove();
    const addBtn = page.querySelector(`[data-add-device="${type}"]`);
    if (
      addBtn?.hasAttribute("data-add-singleton") &&
      !page.querySelector(`[data-new-device="${type}"]:not([hidden]):not([data-new-template="true"])`)
    ) {
      addBtn.disabled = false;
    }
  };

  page.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-hw-toggle]");
    if (toggle) {
      closeAddMenu(event);
      const item = toggle.closest("[data-hw-item]");
      const open = item.classList.toggle("is-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }

    const kebabBtn = event.target.closest("[data-hw-kebab-toggle]");
    if (kebabBtn) {
      closeAddMenu(event);
      const menu = kebabBtn.parentElement.querySelector(".hw-kebab-menu");
      const willOpen = menu.hidden;
      closeKebabs();
      menu.hidden = !willOpen;
      event.stopPropagation();
      return;
    }

    const removeToggle = event.target.closest("[data-hw-remove-toggle]");
    if (removeToggle) {
      closeAddMenu(event);
      const item = removeToggle.closest("[data-hw-item]");
      const flag = item.querySelector(".hw-remove-flag");
      const removed = item.classList.toggle("is-removed");
      if (flag) {
        flag.checked = removed;
      }
      removeToggle.textContent = removed ? "Restore device" : "Remove device";
      closeKebabs();
      return;
    }

    const removeNew = event.target.closest("[data-hw-remove-new]");
    if (removeNew) {
      closeAddMenu(event);
      deactivateNew(removeNew.closest("[data-hw-item]"));
      closeKebabs();
      return;
    }

    const addBtn = event.target.closest("[data-add-device]");
    if (addBtn) {
      activateDevice(addBtn.dataset.addDevice, addBtn);
      closeAddMenu(event);
      return;
    }

    const bootMove = event.target.closest("[data-boot-move]");
    if (bootMove) {
      const row = bootMove.closest("[data-boot-device]");
      const editor = bootMove.closest("[data-boot-order-editor]");
      if (row && editor) {
        if (
          bootMove.dataset.bootMove === "up" &&
          row.previousElementSibling &&
          row.previousElementSibling.matches("[data-boot-device]")
        ) {
          row.parentElement.insertBefore(row, row.previousElementSibling);
        } else if (bootMove.dataset.bootMove === "down" && row.nextElementSibling) {
          row.parentElement.insertBefore(row.nextElementSibling, row);
        }
        syncBootOrder(editor);
      }
      return;
    }

    closeKebabs();
    closeAddMenu(event);
  });

  page.addEventListener("change", (event) => {
    const hotplug = event.target.closest("[data-hotplug-token]");
    if (hotplug) {
      const editor = hotplug.closest("[data-hotplug-editor]");
      if (editor) {
        syncHotplug(editor);
      }
      return;
    }

    const bootEnabled = event.target.closest("[data-boot-enabled]");
    if (bootEnabled) {
      const editor = bootEnabled.closest("[data-boot-order-editor]");
      if (editor) {
        syncBootOrder(editor);
      }
    }
  });

  let draggedBootRow = null;
  page.addEventListener("dragstart", (event) => {
    const row = event.target.closest("[data-boot-device]");
    if (!row || !event.target.closest("[data-boot-order-editor]")) {
      return;
    }
    draggedBootRow = row;
    row.classList.add("is-dragging");
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", row.dataset.bootDevice || "");
    }
  });

  page.addEventListener("dragend", () => {
    if (draggedBootRow) {
      draggedBootRow.classList.remove("is-dragging");
    }
    draggedBootRow = null;
  });

  page.addEventListener("dragover", (event) => {
    if (draggedBootRow && event.target.closest("[data-boot-order-editor]")) {
      event.preventDefault();
    }
  });

  page.addEventListener("drop", (event) => {
    if (!draggedBootRow) {
      return;
    }
    const targetRow = event.target.closest("[data-boot-device]");
    const editor = event.target.closest("[data-boot-order-editor]");
    if (!targetRow || !editor || targetRow === draggedBootRow) {
      return;
    }
    event.preventDefault();
    const box = targetRow.getBoundingClientRect();
    if (event.clientY > box.top + box.height / 2) {
      targetRow.after(draggedBootRow);
    } else {
      targetRow.before(draggedBootRow);
    }
    syncBootOrder(editor);
  });

  document.addEventListener("click", (event) => {
    if (!page.contains(event.target)) {
      closeKebabs();
      const details = page.querySelector("[data-hw-add]");
      if (details && !details.contains(event.target)) {
        details.open = false;
      }
    }
  });

  if (form) {
    form.addEventListener("submit", (event) => {
      page.querySelectorAll("[data-hw-checkbox-shadow]").forEach((shadow) => {
        shadow.remove();
      });
      page
        .querySelectorAll('[data-new-device]:not([hidden]) input[type="checkbox"][data-new-input][name]')
        .forEach((checkbox) => {
          if (!checkbox.checked) {
            const shadow = document.createElement("input");
            shadow.type = "hidden";
            shadow.name = checkbox.name;
            shadow.value = "";
            shadow.setAttribute("data-hw-checkbox-shadow", "true");
            checkbox.before(shadow);
          }
        });

      let invalid = null;
      page.querySelectorAll("[data-new-device]:not([hidden]) [data-new-required]").forEach((el) => {
        el.classList.remove("hw-invalid");
        if (!invalid && !String(el.value).trim()) {
          invalid = el;
        }
      });
      if (invalid) {
        event.preventDefault();
        const virtualTab = document.getElementById("hardware-tab-virtual");
        if (virtualTab) {
          virtualTab.checked = true;
        }
        invalid.closest("[data-hw-item]").classList.add("is-open");
        invalid.classList.add("hw-invalid");
        invalid.focus();
      }
    });
  }

  page.querySelectorAll(".hw-field textarea").forEach(initAutogrowTextarea);
  page.querySelectorAll("[data-boot-order-editor]").forEach(syncBootOrder);
};

export { initHardwareEditor };
