(() => {
  const themeKey = "pve-helper-theme";
  const themeToggle = document.querySelector("[data-theme-toggle]");
  const themeLabels = document.querySelectorAll("[data-theme-label]");
  const menu = document.getElementById("context-menu");
  const recentTasks = document.querySelector("[data-recent-tasks]");
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

  if (recentTasks) {
    const rows = recentTasks.querySelector("[data-task-rows]");
    const previousButton = recentTasks.querySelector("[data-task-prev]");
    const nextButton = recentTasks.querySelector("[data-task-next]");
    const pageLabel = recentTasks.querySelector("[data-task-page-label]");
    const tasksUrl = recentTasks.dataset.tasksUrl;
    const pollMs = Number.parseInt(recentTasks.dataset.taskPollMs || "10000", 10);
    let taskPage = Number.parseInt(recentTasks.dataset.taskPage || "0", 10);
    let loadingTasks = false;

    const escapeHtml = (value) =>
      String(value ?? "").replace(/[&<>"']/g, (char) => {
        const entities = {
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        };
        return entities[char];
      });

    const renderTaskRows = (tasks) => {
      if (!rows) {
        return;
      }

      if (!tasks.length) {
        rows.innerHTML = '<tr><td colspan="9" class="empty-state">No recent tasks.</td></tr>';
        return;
      }

      rows.innerHTML = tasks
        .map(
          (task) => `
            <tr>
              <td>${escapeHtml(task.name)}</td>
              <td>${escapeHtml(task.target)}</td>
              <td><span class="badge ${escapeHtml(task.status_class)}">${escapeHtml(task.status)}</span></td>
              <td>${escapeHtml(task.details)}</td>
              <td>${escapeHtml(task.initiator)}</td>
              <td>${escapeHtml(task.queued_for)}</td>
              <td>${escapeHtml(task.started_at)}</td>
              <td>${escapeHtml(task.finished_at)}</td>
              <td>${escapeHtml(task.server)}</td>
            </tr>
          `
        )
        .join("");
    };

    const updateTaskControls = (data) => {
      taskPage = data.page || 0;
      recentTasks.dataset.taskPage = String(taskPage);

      if (previousButton) {
        previousButton.disabled = !data.has_previous;
      }
      if (nextButton) {
        nextButton.disabled = !data.has_next;
      }
      if (pageLabel) {
        pageLabel.textContent = data.total ? `${data.start_index}-${data.end_index} of ${data.total}` : "0 of 0";
      }
    };

    const loadTaskPage = async (page) => {
      if (!tasksUrl || loadingTasks) {
        return;
      }

      loadingTasks = true;
      try {
        const url = new URL(tasksUrl, window.location.origin);
        url.searchParams.set("page", String(Math.max(0, page)));
        const response = await fetch(url, {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        renderTaskRows(data.tasks || []);
        updateTaskControls(data);
      } catch (error) {
        // Recent task refresh is best effort; the server-rendered rows remain usable.
      } finally {
        loadingTasks = false;
      }
    };

    if (previousButton) {
      previousButton.addEventListener("click", () => {
        loadTaskPage(taskPage - 1);
      });
    }

    if (nextButton) {
      nextButton.addEventListener("click", () => {
        loadTaskPage(taskPage + 1);
      });
    }

    window.setInterval(() => {
      if (taskPage === 0 && document.visibilityState !== "hidden") {
        loadTaskPage(0);
      }
    }, Number.isFinite(pollMs) ? pollMs : 10000);
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
