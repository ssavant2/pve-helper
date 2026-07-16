import { CONSOLE_KEY_ROWS, CONSOLE_MODIFIER_KEYSYMS } from "./register.js";
import {
  consoleKeepaliveKey,
  consoleLayoutKey,
  consoleReconnectPrefix,
  recentTasksRefreshEvent,
  registerPageCleanup,
} from "./shell.js";

const buildConsoleKeyIndex = (rows) => {
  const index = {};
  rows.forEach(([code, base, shift, altgr]) => {
    if (altgr) index[altgr] = { code, mods: ["AltRight"] };
    if (shift) index[shift] = { code, mods: ["ShiftLeft"] };
    if (base) index[base] = { code, mods: [] };
  });
  return index;
};

const CONSOLE_KEY_INDEX = Object.fromEntries(
  Object.entries(CONSOLE_KEY_ROWS).map(([id, rows]) => [id, buildConsoleKeyIndex(rows)])
);

const CONSOLE_CONTROL_KEYS = {
  "\n": [0xff0d, "Enter"],
  "\r": [0xff0d, "Enter"],
  "\t": [0xff09, "Tab"],
  "\b": [0xff08, "Backspace"],
  "\u001b": [0xff1b, "Escape"],
};

const initConsolePages = (root) => {
  root.querySelectorAll("[data-console-page]").forEach((page) => {
    if (page.dataset.initialized === "true") {
      return;
    }
    page.dataset.initialized = "true";

    const connectButton = page.querySelector("[data-console-connect]");
    const disconnectButton = page.querySelector("[data-console-disconnect]");
    const frame = page.querySelector("[data-console-frame]");
    const sideMenu = page.querySelector("[data-console-side-menu]");
    const screen = page.querySelector("[data-console-screen]");
    const status = page.querySelector("[data-console-status]");
    const keepaliveInput = page.querySelector("[data-console-keepalive-minutes]");
    const layoutSelect = page.querySelector("[data-console-keyboard-layout]");
    let rfb = null;
    let terminal = null;
    let terminalFitAddon = null;
    let terminalSocket = null;
    let resizeObserver = null;
    let connectedAtLeastOnce = false;

    const reconnectKey = `${consoleReconnectPrefix}:${page.dataset.sessionUrl || window.location.pathname}`;
    const keepaliveMinutes = () => {
      const parsed = Number.parseInt(keepaliveInput?.value || "10", 10);
      return Number.isNaN(parsed) ? 10 : Math.min(99, Math.max(1, parsed));
    };

    const saveKeepaliveMinutes = () => {
      if (!keepaliveInput) {
        return;
      }
      const minutes = keepaliveMinutes();
      keepaliveInput.value = String(minutes);
      try {
        localStorage.setItem(consoleKeepaliveKey, String(minutes));
      } catch (_error) {
        // Local storage can be unavailable in restrictive browser modes.
      }
    };

    const restoreKeepaliveMinutes = () => {
      if (!keepaliveInput) {
        return;
      }
      try {
        const stored = Number.parseInt(localStorage.getItem(consoleKeepaliveKey) || "", 10);
        if (!Number.isNaN(stored)) {
          keepaliveInput.value = String(Math.min(99, Math.max(1, stored)));
        }
      } catch (_error) {
        // Keep the template default.
      }
    };

    const currentKeyboardLayout = () => {
      const value = layoutSelect?.value || "";
      return CONSOLE_KEY_INDEX[value] ? value : "en-us";
    };

    const saveKeyboardLayout = () => {
      try {
        localStorage.setItem(consoleLayoutKey, currentKeyboardLayout());
      } catch (_error) {
        // Local storage can be unavailable in restrictive browser modes.
      }
    };

    const restoreKeyboardLayout = () => {
      if (!layoutSelect) {
        return;
      }
      try {
        const stored = localStorage.getItem(consoleLayoutKey) || "";
        if (CONSOLE_KEY_INDEX[stored]) {
          layoutSelect.value = stored;
        }
      } catch (_error) {
        // Keep the template default.
      }
    };

    const rememberReconnectWindow = () => {
      if (!connectedAtLeastOnce) {
        return;
      }
      try {
        localStorage.setItem(reconnectKey, JSON.stringify({ until: Date.now() + keepaliveMinutes() * 60 * 1000 }));
      } catch (_error) {
        // Local storage can be unavailable in restrictive browser modes.
      }
    };

    const clearReconnectWindow = () => {
      try {
        localStorage.removeItem(reconnectKey);
      } catch (_error) {
        // Local storage can be unavailable in restrictive browser modes.
      }
    };

    const shouldAutoReconnect = () => {
      try {
        const raw = localStorage.getItem(reconnectKey);
        if (!raw) {
          return false;
        }
        const record = JSON.parse(raw);
        if (!record || Number(record.until) <= Date.now()) {
          localStorage.removeItem(reconnectKey);
          return false;
        }
        return true;
      } catch (_error) {
        return false;
      }
    };

    const applySetting = (input) => {
      if (!rfb) {
        return;
      }
      const key = input.dataset.consoleSetting;
      if (!key) {
        return;
      }
      if (input.type === "checkbox") {
        rfb[key] = input.checked;
      } else {
        rfb[key] = Number(input.value);
      }
    };

    const applySettings = () => {
      page.querySelectorAll("[data-console-setting]").forEach(applySetting);
    };

    const loadStylesheetOnce = (url) =>
      new Promise((resolve, reject) => {
        if (!url) {
          resolve();
          return;
        }
        const existing = document.querySelector(`link[data-console-css="${CSS.escape(url)}"]`);
        if (existing) {
          resolve();
          return;
        }
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = url;
        link.dataset.consoleCss = url;
        link.addEventListener("load", resolve, { once: true });
        link.addEventListener("error", reject, { once: true });
        document.head.appendChild(link);
      });

    const loadScriptOnce = (url) =>
      new Promise((resolve, reject) => {
        if (!url) {
          reject(new Error("Missing console script URL."));
          return;
        }
        const existing = document.querySelector(`script[data-console-script="${CSS.escape(url)}"]`);
        if (existing) {
          if (existing.dataset.loaded === "true") {
            resolve();
          } else {
            existing.addEventListener("load", resolve, { once: true });
            existing.addEventListener("error", reject, { once: true });
          }
          return;
        }
        const script = document.createElement("script");
        script.src = url;
        script.dataset.consoleScript = url;
        script.addEventListener(
          "load",
          () => {
            script.dataset.loaded = "true";
            resolve();
          },
          { once: true }
        );
        script.addEventListener("error", reject, { once: true });
        document.head.appendChild(script);
      });

    const nudgeConsoleResize = () => {
      if (!rfb) {
        if (terminalFitAddon && terminalSocket?.readyState === WebSocket.OPEN) {
          terminalFitAddon.fit();
        }
        return;
      }
      const scaleInput = page.querySelector('[data-console-setting="scaleViewport"]');
      if (scaleInput) {
        rfb.scaleViewport = scaleInput.checked;
      }
      window.dispatchEvent(new Event("resize"));
    };

    const setStatus = (message, connected = false) => {
      if (status) {
        status.textContent = message;
      }
      page.classList.toggle("console-connected", connected);
      if (connectButton) {
        connectButton.disabled = connected;
      }
      if (disconnectButton) {
        disconnectButton.disabled = !connected;
      }
    };

    const hidePanels = () => {
      page.querySelectorAll("[data-console-panel]").forEach((panel) => {
        panel.hidden = true;
      });
      page.querySelectorAll("[data-console-panel-toggle]").forEach((button) => {
        button.classList.remove("active");
      });
    };

    const disconnect = ({ remember = false } = {}) => {
      if (remember) {
        rememberReconnectWindow();
      } else {
        clearReconnectWindow();
      }
      if (rfb) {
        rfb.disconnect();
        rfb = null;
      }
      if (terminalSocket) {
        terminalSocket.close();
        terminalSocket = null;
      }
      if (terminal) {
        terminal.dispose();
        terminal = null;
        terminalFitAddon = null;
      }
      if (screen) {
        screen.innerHTML = "";
        screen.classList.remove("xterm-screen");
      }
      setStatus("Disconnected", false);
    };

    const buildConsoleWebSocketUrl = (payload) => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const wsUrl = new URL(payload.websocket_url, window.location.origin);
      wsUrl.protocol = protocol;
      return wsUrl;
    };

    const markConnected = () => {
      connectedAtLeastOnce = true;
      rememberReconnectWindow();
      setStatus("Connected", true);
      nudgeConsoleResize();
    };

    const resizeTerminal = () => {
      if (!terminalFitAddon || !terminal || terminalSocket?.readyState !== WebSocket.OPEN) {
        return;
      }
      terminalFitAddon.fit();
      terminalSocket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
    };

    const connectXterm = async (payload) => {
      await loadStylesheetOnce(page.dataset.xtermCssUrl || "");
      await loadScriptOnce(page.dataset.xtermJsUrl || "");
      await loadScriptOnce(page.dataset.xtermFitUrl || "");
      const TerminalCtor = window.Terminal?.Terminal || window.Terminal;
      const FitAddonCtor = window.FitAddon?.FitAddon || window.FitAddon;
      if (!TerminalCtor || !FitAddonCtor) {
        throw new Error("xterm.js failed to load.");
      }
      screen.innerHTML = "";
      screen.classList.add("xterm-screen");
      terminal = new TerminalCtor({
        cursorBlink: true,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
        fontSize: 14,
        scrollback: 5000,
        convertEol: true,
        theme: {
          background: "#000000",
          foreground: "#d8d8d8",
          cursor: "#e5edf3",
          selectionBackground: "#2d5d8f",
        },
      });
      terminalFitAddon = new FitAddonCtor();
      terminal.loadAddon(terminalFitAddon);
      terminal.open(screen);
      terminal.onData((data) => {
        if (terminalSocket?.readyState === WebSocket.OPEN) {
          terminalSocket.send(JSON.stringify({ type: "data", data }));
        }
      });
      terminal.onResize((size) => {
        if (terminalSocket?.readyState === WebSocket.OPEN) {
          terminalSocket.send(JSON.stringify({ type: "resize", cols: size.cols, rows: size.rows }));
        }
      });
      terminalSocket = new WebSocket(buildConsoleWebSocketUrl(payload).href);
      terminalSocket.binaryType = "arraybuffer";
      terminalSocket.addEventListener("open", () => {
        markConnected();
        window.setTimeout(() => {
          resizeTerminal();
          terminal?.focus();
        }, 50);
      });
      terminalSocket.addEventListener("message", (event) => {
        if (event.data instanceof ArrayBuffer) {
          terminal?.write(new Uint8Array(event.data));
        } else {
          terminal?.write(String(event.data || ""));
        }
      });
      terminalSocket.addEventListener("close", () => {
        terminalSocket = null;
        setStatus("Disconnected", false);
      });
      terminalSocket.addEventListener("error", () => {
        setStatus("Console disconnected", false);
      });
    };

    const connectNovnc = async (payload) => {
      const module = await import(page.dataset.novncUrl);
      const RFB = module.default;

      screen.innerHTML = "";
      screen.classList.remove("xterm-screen");
      rfb = new RFB(screen, buildConsoleWebSocketUrl(payload).href, {
        credentials: { password: payload.password || "" },
      });
      applySettings();
      rfb.focusOnClick = true;
      rfb.addEventListener("connect", markConnected);
      rfb.addEventListener("disconnect", (event) => {
        rfb = null;
        const clean = event.detail?.clean;
        setStatus(clean ? "Disconnected" : "Console disconnected", false);
      });
      rfb.addEventListener("securityfailure", (event) => {
        setStatus(event.detail?.reason || "Console security negotiation failed.", false);
      });
    };

    const readClipboardText = async () => {
      if (navigator.clipboard?.readText) {
        try {
          return await navigator.clipboard.readText();
        } catch (_error) {
          // Fall back below when the browser blocks clipboard permissions.
        }
      }
      return window.prompt("Paste text") || "";
    };

    const sendNoVncKeyStroke = (spec, keysym) => {
      const mods = spec.mods || [];
      mods.forEach((code) => {
        rfb.sendKey(CONSOLE_MODIFIER_KEYSYMS[code], code, true);
      });
      rfb.sendKey(keysym, spec.code, true);
      rfb.sendKey(keysym, spec.code, false);
      mods
        .slice()
        .reverse()
        .forEach((code) => {
          rfb.sendKey(CONSOLE_MODIFIER_KEYSYMS[code], code, false);
        });
    };

    const sendNoVncText = async (text) => {
      if (!rfb || !text) {
        return;
      }
      const index = CONSOLE_KEY_INDEX[currentKeyboardLayout()] || CONSOLE_KEY_INDEX["en-us"];
      for (const char of text) {
        const control = CONSOLE_CONTROL_KEYS[char];
        if (control) {
          rfb.sendKey(control[0], control[1]);
        } else {
          const cp = char.codePointAt(0);
          const keysym = cp > 0xff ? 0x01000000 + cp : cp;
          const spec = index[char];
          if (spec) {
            // Physical-key path: bypasses QEMU's VNC keymap entirely.
            sendNoVncKeyStroke(spec, keysym);
          } else if (keysym) {
            // Fallback for unmapped characters (e.g. dead-key glyphs).
            rfb.sendKey(keysym);
          }
        }
        await new Promise((resolve) => window.setTimeout(resolve, 5));
      }
    };

    const pasteClipboard = async () => {
      const text = await readClipboardText();
      if (!text) {
        return;
      }
      if (terminalSocket?.readyState === WebSocket.OPEN) {
        terminalSocket.send(JSON.stringify({ type: "data", data: text }));
        return;
      }
      if (rfb) {
        await sendNoVncText(text);
      }
    };

    const connect = async () => {
      if (!screen || !page.dataset.sessionUrl || !page.dataset.novncUrl) {
        return;
      }

      setStatus("Creating console session...", false);
      try {
        const response = await fetch(new URL(page.dataset.sessionUrl, window.location.origin), {
          method: "POST",
          headers: {
            Accept: "application/json",
            "X-CSRFToken": page.dataset.csrfToken || "",
          },
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Console session failed.");
        }

        if (payload.console_type === "xterm") {
          await connectXterm(payload);
        } else {
          await connectNovnc(payload);
        }
        setStatus("Connecting...", true);
        nudgeConsoleResize();
      } catch (error) {
        disconnect();
        setStatus(error.message || "Console connection failed.", false);
      }
    };

    const submitPowerAction = async (action) => {
      if (!page.dataset.powerUrl) {
        return;
      }
      const body = new URLSearchParams();
      body.set("action", action);
      setStatus(`Submitting ${action}...`, Boolean(rfb));
      const response = await fetch(new URL(page.dataset.powerUrl, window.location.origin), {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "X-CSRFToken": page.dataset.csrfToken || "",
          "X-Requested-With": "fetch",
        },
        body,
      });
      if (!response.ok) {
        setStatus(`${action} failed`, Boolean(rfb));
        return;
      }
      setStatus(`${action} submitted`, Boolean(rfb));
      window.dispatchEvent(new Event(recentTasksRefreshEvent));
    };

    sideMenu?.querySelector("[data-console-menu-tab]")?.addEventListener("click", () => {
      sideMenu.classList.toggle("collapsed");
      hidePanels();
    });

    sideMenu?.querySelectorAll("[data-console-panel-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        const targetPanel = page.querySelector(
          `[data-console-panel="${CSS.escape(button.dataset.consolePanelToggle || "")}"]`
        );
        const shouldOpen = !targetPanel || targetPanel.hidden;
        hidePanels();
        if (targetPanel && shouldOpen) {
          targetPanel.hidden = false;
          button.classList.add("active");
        }
      });
    });

    page.querySelectorAll("[data-console-setting]").forEach((input) => {
      input.addEventListener("input", () => applySetting(input));
      input.addEventListener("change", () => applySetting(input));
    });
    restoreKeepaliveMinutes();
    keepaliveInput?.addEventListener("input", saveKeepaliveMinutes);
    keepaliveInput?.addEventListener("change", saveKeepaliveMinutes);
    restoreKeyboardLayout();
    layoutSelect?.addEventListener("change", saveKeyboardLayout);

    page.querySelectorAll("[data-console-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.consoleAction;
        if (action === "disconnect") {
          disconnect();
          return;
        }
        if (action === "reload") {
          disconnect({ remember: true });
          hidePanels();
          await connect();
          return;
        }
        if (action === "fullscreen" && frame) {
          if (document.fullscreenElement) {
            await document.exitFullscreen();
          } else {
            await frame.requestFullscreen();
          }
          return;
        }
        if (action === "ctrl-alt-del") {
          if (rfb) {
            rfb.sendCtrlAltDel();
          }
          hidePanels();
          return;
        }
        if (action === "paste-clipboard") {
          await pasteClipboard();
          hidePanels();
        }
      });
    });

    page.querySelectorAll("[data-console-power-action]").forEach((button) => {
      button.addEventListener("click", async () => {
        hidePanels();
        await submitPowerAction(button.dataset.consolePowerAction || "");
      });
    });

    const closePanelsOnOutsideClick = (event) => {
      if (sideMenu && !sideMenu.contains(event.target)) {
        hidePanels();
      }
    };
    document.addEventListener("click", closePanelsOnOutsideClick);
    registerPageCleanup(() => document.removeEventListener("click", closePanelsOnOutsideClick));

    disconnectButton?.addEventListener("click", () => disconnect());
    registerPageCleanup(() => disconnect({ remember: true }));
    if (frame && window.ResizeObserver) {
      resizeObserver = new ResizeObserver(nudgeConsoleResize);
      resizeObserver.observe(frame);
      registerPageCleanup(() => resizeObserver?.disconnect());
    }

    connectButton?.addEventListener("click", connect);
    if (shouldAutoReconnect() && connectButton && !connectButton.disabled) {
      window.setTimeout(connect, 50);
    }
  });
};

export { buildConsoleKeyIndex, CONSOLE_CONTROL_KEYS, CONSOLE_KEY_INDEX, initConsolePages };
