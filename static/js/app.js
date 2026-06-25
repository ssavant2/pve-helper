(() => {
  const themeKey = "pve-helper-theme";
  const themeToggle = document.querySelector("[data-theme-toggle]");
  const themeLabels = document.querySelectorAll("[data-theme-label]");
  const menu = document.getElementById("context-menu");
  let activeLabel = "";

  const preferredTheme = () => {
    try {
      const storedTheme = localStorage.getItem(themeKey);
      if (storedTheme === "light" || storedTheme === "dark") {
        return storedTheme;
      }
    } catch (error) {
      return "light";
    }

    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };

  const applyTheme = (theme) => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    themeLabels.forEach((label) => {
      label.textContent = theme === "dark" ? "Dark" : "Light";
    });
    if (themeToggle) {
      themeToggle.setAttribute("aria-label", `Switch to ${theme === "dark" ? "light" : "dark"} theme`);
    }
  };

  applyTheme(preferredTheme());

  if (window.lucide) {
    window.lucide.createIcons({
      attrs: {
        "aria-hidden": "true",
      },
    });
  }

  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const currentTheme = document.documentElement.dataset.theme === "dark" ? "dark" : "light";
      const nextTheme = currentTheme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(themeKey, nextTheme);
      } catch (error) {
        // Theme persistence is optional; the UI still updates for this page.
      }
      applyTheme(nextTheme);
    });
  }

  if (menu) {
    document.addEventListener("contextmenu", (event) => {
      const row = event.target.closest("[data-context-label]");
      if (!row) {
        return;
      }

      event.preventDefault();
      activeLabel = row.dataset.contextLabel || "";
      menu.style.left = `${event.clientX}px`;
      menu.style.top = `${event.clientY}px`;
      menu.hidden = false;
    });

    document.addEventListener("click", (event) => {
      if (!menu.contains(event.target)) {
        menu.hidden = true;
      }
    });

    menu.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) {
        return;
      }

      if (button.dataset.action === "copy-path" && activeLabel) {
        await navigator.clipboard.writeText(activeLabel);
      }

      menu.hidden = true;
    });
  }
})();
