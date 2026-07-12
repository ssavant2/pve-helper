import { createIcons } from "./shell.js";

const initVmRegister = (root = document) => {
  const page = root.querySelector ? root.querySelector("[data-vm-register]") : null;
  if (!page || page.dataset.initialized === "true") {
    return;
  }
  page.dataset.initialized = "true";

  // Firmware: reveal the EFI fields for OVMF and keep the summary in sync.
  const bios = page.querySelector("[data-vmreg-bios]");
  const machine = page.querySelector("select[name='machine']");
  const efi = page.querySelector("[data-vmreg-efi]");
  const summary = page.querySelector("[data-vmreg-firmware-summary]");
  const optionLabel = (select) => select?.options[select.selectedIndex]?.textContent.trim() || "";
  const syncFirmware = () => {
    if (efi) {
      efi.hidden = bios?.value !== "ovmf";
    }
    if (summary) {
      summary.textContent = `${optionLabel(bios)} · ${optionLabel(machine)}`;
    }
  };
  bios?.addEventListener("change", syncFirmware);
  machine?.addEventListener("change", syncFirmware);
  syncFirmware();

  // Network adapters: add / remove rows and keep names contiguous (nic0_*, ...).
  const nics = page.querySelector("[data-vmreg-nics]");
  const template = page.querySelector("[data-vmreg-nic-template]");
  const reindexNics = () => {
    const fields = ["model", "bridge", "vlan"];
    nics?.querySelectorAll("[data-vmreg-nic]").forEach((row, index) => {
      row.querySelectorAll("select, input").forEach((control, position) => {
        if (fields[position]) {
          control.name = `nic${index}_${fields[position]}`;
        }
      });
    });
  };

  page.addEventListener("click", (event) => {
    const addButton = event.target.closest("[data-vmreg-add-nic]");
    if (addButton && template && nics) {
      nics.appendChild(template.content.cloneNode(true));
      reindexNics();
      createIcons();
      return;
    }
    const removeButton = event.target.closest("[data-vmreg-remove-nic]");
    if (removeButton) {
      removeButton.closest("[data-vmreg-nic]")?.remove();
      reindexNics();
    }
  });
  reindexNics();
};

// Physical-key paste for noVNC: map pasted characters to hardware keys and
// send them via the QEMU Extended Key Event (scancode) path, bypassing
// QEMU's own VNC keymap so national characters land correctly on the guest.
const CONSOLE_MODIFIER_KEYSYMS = {
  ShiftLeft: 0xffe1,
  AltRight: 0xffea,
};

const CONSOLE_LETTER_ROWS = "abcdefghijklmnopqrstuvwxyz"
  .split("")
  .map((letter) => [`Key${letter.toUpperCase()}`, letter, letter.toUpperCase()]);

// German QWERTZ: y/z swapped, plus @ EUR MU on AltGr.
const CONSOLE_LETTER_ROWS_DE = CONSOLE_LETTER_ROWS.map((row) => {
  const overrides = {
    KeyY: ["KeyY", "z", "Z"],
    KeyZ: ["KeyZ", "y", "Y"],
    KeyQ: ["KeyQ", "q", "Q", "@"],
    KeyE: ["KeyE", "e", "E", "€"],
    KeyM: ["KeyM", "m", "M", "µ"],
  };
  return overrides[row[0]] || row;
});

// Nordic layouts share this digit row + the key right of "0".
const CONSOLE_NORDIC_DIGITS = [
  ["Digit1", "1", "!"],
  ["Digit2", "2", '"', "@"],
  ["Digit3", "3", "#", "£"],
  ["Digit4", "4", "¤", "$"],
  ["Digit5", "5", "%", "€"],
  ["Digit6", "6", "&"],
  ["Digit7", "7", "/", "{"],
  ["Digit8", "8", "(", "["],
  ["Digit9", "9", ")", "]"],
  ["Digit0", "0", "=", "}"],
  ["Minus", "+", "?", "\\"],
];

// Each row: [DOM code, base char, shifted char?, AltGr char?]
const CONSOLE_KEY_ROWS = {
  "en-us": [
    ...CONSOLE_LETTER_ROWS,
    ["Digit1", "1", "!"],
    ["Digit2", "2", "@"],
    ["Digit3", "3", "#"],
    ["Digit4", "4", "$"],
    ["Digit5", "5", "%"],
    ["Digit6", "6", "^"],
    ["Digit7", "7", "&"],
    ["Digit8", "8", "*"],
    ["Digit9", "9", "("],
    ["Digit0", "0", ")"],
    ["Minus", "-", "_"],
    ["Equal", "=", "+"],
    ["BracketLeft", "[", "{"],
    ["BracketRight", "]", "}"],
    ["Backslash", "\\", "|"],
    ["Semicolon", ";", ":"],
    ["Quote", "'", '"'],
    ["Backquote", "`", "~"],
    ["Comma", ",", "<"],
    ["Period", ".", ">"],
    ["Slash", "/", "?"],
    ["Space", " "],
  ],
  "en-gb": [
    ...CONSOLE_LETTER_ROWS,
    ["Digit1", "1", "!"],
    ["Digit2", "2", '"'],
    ["Digit3", "3", "£"],
    ["Digit4", "4", "$", "€"],
    ["Digit5", "5", "%"],
    ["Digit6", "6", "^"],
    ["Digit7", "7", "&"],
    ["Digit8", "8", "*"],
    ["Digit9", "9", "("],
    ["Digit0", "0", ")"],
    ["Minus", "-", "_"],
    ["Equal", "=", "+"],
    ["BracketLeft", "[", "{"],
    ["BracketRight", "]", "}"],
    ["Backslash", "#", "~"],
    ["Semicolon", ";", ":"],
    ["Quote", "'", "@"],
    ["Backquote", "`", "¬"],
    ["Comma", ",", "<"],
    ["Period", ".", ">"],
    ["Slash", "/", "?"],
    ["IntlBackslash", "\\", "|"],
    ["Space", " "],
  ],
  de: [
    ...CONSOLE_LETTER_ROWS_DE,
    ["Digit1", "1", "!"],
    ["Digit2", "2", '"'],
    ["Digit3", "3", "§"],
    ["Digit4", "4", "$"],
    ["Digit5", "5", "%"],
    ["Digit6", "6", "&"],
    ["Digit7", "7", "/", "{"],
    ["Digit8", "8", "(", "["],
    ["Digit9", "9", ")", "]"],
    ["Digit0", "0", "=", "}"],
    ["Minus", "ß", "?", "\\"],
    ["BracketLeft", "ü", "Ü"],
    ["BracketRight", "+", "*", "~"],
    ["Semicolon", "ö", "Ö"],
    ["Quote", "ä", "Ä"],
    ["Backslash", "#", "'"],
    ["IntlBackslash", "<", ">", "|"],
    ["Comma", ",", ";"],
    ["Period", ".", ":"],
    ["Slash", "-", "_"],
    ["Space", " "],
  ],
  sv: [
    ...CONSOLE_LETTER_ROWS,
    ...CONSOLE_NORDIC_DIGITS,
    ["BracketLeft", "å", "Å"],
    ["Semicolon", "ö", "Ö"],
    ["Quote", "ä", "Ä"],
    ["Backslash", "'", "*"],
    ["IntlBackslash", "<", ">", "|"],
    ["Comma", ",", ";"],
    ["Period", ".", ":"],
    ["Slash", "-", "_"],
    ["Backquote", "§", "½"],
    ["Space", " "],
  ],
  no: [
    ...CONSOLE_LETTER_ROWS,
    ...CONSOLE_NORDIC_DIGITS,
    ["BracketLeft", "å", "Å"],
    ["Semicolon", "ø", "Ø"],
    ["Quote", "æ", "Æ"],
    ["Backslash", "'", "*"],
    ["IntlBackslash", "<", ">"],
    ["Comma", ",", ";"],
    ["Period", ".", ":"],
    ["Slash", "-", "_"],
    ["Backquote", "|", "§"],
    ["Space", " "],
  ],
  da: [
    ...CONSOLE_LETTER_ROWS,
    ...CONSOLE_NORDIC_DIGITS,
    ["BracketLeft", "å", "Å"],
    ["Semicolon", "æ", "Æ"],
    ["Quote", "ø", "Ø"],
    ["Backslash", "'", "*"],
    ["IntlBackslash", "<", ">", "\\"],
    ["Comma", ",", ";"],
    ["Period", ".", ":"],
    ["Slash", "-", "_"],
    ["Backquote", "½", "§"],
    ["Space", " "],
  ],
};

// Finnish uses the same physical layout as Swedish.
CONSOLE_KEY_ROWS.fi = CONSOLE_KEY_ROWS.sv;

export {
  CONSOLE_KEY_ROWS,
  CONSOLE_LETTER_ROWS,
  CONSOLE_LETTER_ROWS_DE,
  CONSOLE_MODIFIER_KEYSYMS,
  CONSOLE_NORDIC_DIGITS,
  initVmRegister,
};
