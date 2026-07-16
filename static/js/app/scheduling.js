import { openConfirmDialog } from "./dialogs.js";
import { loadSoftNavigation } from "./navigation.js";
import { escapeHtml, recentTasksRefreshEvent, registerPageCleanup, renderGuestLabel } from "./shell.js";

const initAutoSubmitForms = (root = document) => {
  root.querySelectorAll("[data-auto-submit-form]").forEach((form) => {
    if (form.dataset.initialized === "true") {
      return;
    }

    form.dataset.initialized = "true";
    form.querySelectorAll("[data-auto-submit-control]").forEach((control) => {
      control.addEventListener("change", () => {
        if (form.reportValidity && !form.reportValidity()) {
          return;
        }

        if (form.requestSubmit) {
          form.requestSubmit();
          return;
        }
        form.submit();
      });
    });
  });
};

const initAuditExportDialog = (root = document) => {
  const dialog =
    root.querySelector("[data-audit-export-dialog]") || document.querySelector("[data-audit-export-dialog]");
  if (!dialog || dialog.dataset.initialized === "true") {
    return;
  }

  dialog.dataset.initialized = "true";
  const close = () => {
    dialog.querySelector("[data-audit-date-modal]")?.setAttribute("hidden", "");
    dialog.close();
  };
  document.querySelectorAll("[data-audit-export-open]").forEach((button) => {
    button.addEventListener("click", () => {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    });
  });
  dialog.querySelector("[data-audit-export-close]")?.addEventListener("click", close);
  dialog.querySelector("[data-audit-export-cancel]")?.addEventListener("click", close);
  const padDatePart = (value) => String(value).padStart(2, "0");
  const monthNames = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
  ];
  const weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const clampNumber = (value, min, max) => {
    const parsed = Number.parseInt(value, 10);
    if (Number.isNaN(parsed)) {
      return min;
    }
    return Math.min(max, Math.max(min, parsed));
  };
  const parseExportDate = (value) => {
    const match = String(value || "")
      .trim()
      .match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (!match) {
      return null;
    }
    const parsed = new Date(
      Number.parseInt(match[1], 10),
      Number.parseInt(match[2], 10) - 1,
      Number.parseInt(match[3], 10),
      Number.parseInt(match[4], 10),
      Number.parseInt(match[5], 10)
    );
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  };
  const formatExportDate = (date) =>
    `${date.getFullYear()}-${padDatePart(date.getMonth() + 1)}-${padDatePart(date.getDate())} ${padDatePart(
      date.getHours()
    )}:${padDatePart(date.getMinutes())}`;
  const dateModal = dialog.querySelector("[data-audit-date-modal]");
  const datePanel = dialog.querySelector("[data-audit-date-panel]");
  let activeDateGroup = null;
  let pickerDate = new Date();
  pickerDate.setSeconds(0, 0);
  let pickerYear = pickerDate.getFullYear();
  let pickerMonth = pickerDate.getMonth();

  const syncDateTimeGroup = (group) => {
    const valueInput = group.querySelector("[data-audit-export-datetime-value]");
    const displayInput = group.querySelector("[data-audit-export-display]");
    if (!valueInput || !displayInput) {
      return;
    }
    const parsed = parseExportDate(displayInput.value);
    if (!parsed) {
      valueInput.value = "";
      return;
    }
    const normalized = formatExportDate(parsed);
    displayInput.value = normalized;
    valueInput.value = normalized;
  };
  const closeDatePicker = () => {
    if (dateModal) {
      dateModal.hidden = true;
    }
  };
  const renderDatePicker = () => {
    if (!datePanel || !activeDateGroup) {
      return;
    }
    const firstOfMonth = new Date(pickerYear, pickerMonth, 1);
    const mondayOffset = (firstOfMonth.getDay() + 6) % 7;
    const daysInMonth = new Date(pickerYear, pickerMonth + 1, 0).getDate();
    const today = new Date();
    const dayCells = [];
    for (let index = 0; index < mondayOffset; index += 1) {
      dayCells.push('<span class="audit-date-picker-empty"></span>');
    }
    for (let day = 1; day <= daysInMonth; day += 1) {
      const isSelected =
        pickerDate.getFullYear() === pickerYear &&
        pickerDate.getMonth() === pickerMonth &&
        pickerDate.getDate() === day;
      const isToday = today.getFullYear() === pickerYear && today.getMonth() === pickerMonth && today.getDate() === day;
      dayCells.push(
        `<button type="button" class="audit-date-picker-day${isSelected ? " is-selected" : ""}${
          isToday ? " is-today" : ""
        }" data-audit-date-day="${day}">${day}</button>`
      );
    }
    datePanel.innerHTML = `
        <div class="audit-date-picker-title">
          <strong>${activeDateGroup.dataset.auditExportLabel || "Date range"}</strong>
          <button type="button" aria-label="Close date picker" data-audit-date-close>x</button>
        </div>
        <div class="audit-date-picker-header">
          <button type="button" aria-label="Previous month" data-audit-date-prev><i data-lucide="chevron-left" aria-hidden="true"></i></button>
          <strong>${monthNames[pickerMonth]} ${pickerYear}</strong>
          <button type="button" aria-label="Next month" data-audit-date-next><i data-lucide="chevron-right" aria-hidden="true"></i></button>
        </div>
        <div class="audit-date-picker-grid">
          ${weekdays.map((day) => `<span class="audit-date-picker-weekday">${day}</span>`).join("")}
          ${dayCells.join("")}
        </div>
        <div class="audit-date-picker-time">
          <label>Hour <input type="number" min="0" max="23" step="1" value="${padDatePart(pickerDate.getHours())}" data-audit-date-hour></label>
          <label>Minute <input type="number" min="0" max="59" step="1" value="${padDatePart(pickerDate.getMinutes())}" data-audit-date-minute></label>
        </div>
        <div class="audit-date-picker-actions">
          <button type="button" data-audit-date-clear>Clear</button>
          <button type="button" data-audit-date-apply>Apply</button>
        </div>
      `;
    if (window.lucide) {
      window.lucide.createIcons({ attrs: { "stroke-width": 2 } });
    }
  };

  dialog.querySelectorAll("[data-audit-export-datetime]").forEach((group) => {
    const openButton = group.querySelector("[data-audit-export-open-picker]");
    const displayInput = group.querySelector("[data-audit-export-display]");
    openButton?.addEventListener("click", () => {
      activeDateGroup = group;
      const parsed = parseExportDate(displayInput?.value);
      pickerDate = parsed || new Date();
      pickerDate.setSeconds(0, 0);
      if (!parsed) {
        pickerDate.setHours(
          clampNumber(group.dataset.auditExportDefaultHour || "00", 0, 23),
          clampNumber(group.dataset.auditExportDefaultMinute || "00", 0, 59),
          0,
          0
        );
      }
      pickerYear = pickerDate.getFullYear();
      pickerMonth = pickerDate.getMonth();
      renderDatePicker();
      if (dateModal) {
        dateModal.hidden = false;
      }
    });
  });

  dialog.querySelector(".audit-export-form")?.addEventListener("submit", () => {
    dialog.querySelectorAll("[data-audit-export-datetime]").forEach(syncDateTimeGroup);
  });
  dateModal?.addEventListener("click", (event) => {
    if (event.target === dateModal) {
      closeDatePicker();
    }
  });
  datePanel?.addEventListener("click", (event) => {
    event.stopPropagation();
    const target = event.target instanceof Element ? event.target.closest("button") : null;
    if (!target) {
      return;
    }
    if (target.matches("[data-audit-date-close]")) {
      closeDatePicker();
      return;
    }
    if (target.matches("[data-audit-date-prev]")) {
      pickerMonth -= 1;
      if (pickerMonth < 0) {
        pickerMonth = 11;
        pickerYear -= 1;
      }
      renderDatePicker();
      return;
    }
    if (target.matches("[data-audit-date-next]")) {
      pickerMonth += 1;
      if (pickerMonth > 11) {
        pickerMonth = 0;
        pickerYear += 1;
      }
      renderDatePicker();
      return;
    }
    if (target.matches("[data-audit-date-day]")) {
      pickerDate.setFullYear(pickerYear, pickerMonth, Number.parseInt(target.dataset.auditDateDay, 10));
      renderDatePicker();
      return;
    }
    if (target.matches("[data-audit-date-clear]")) {
      const valueInput = activeDateGroup?.querySelector("[data-audit-export-datetime-value]");
      const displayInput = activeDateGroup?.querySelector("[data-audit-export-display]");
      if (valueInput) {
        valueInput.value = "";
      }
      if (displayInput) {
        displayInput.value = "";
      }
      closeDatePicker();
      return;
    }
    if (target.matches("[data-audit-date-apply]")) {
      const hourInput = datePanel.querySelector("[data-audit-date-hour]");
      const minuteInput = datePanel.querySelector("[data-audit-date-minute]");
      pickerDate.setHours(clampNumber(hourInput?.value, 0, 23), clampNumber(minuteInput?.value, 0, 59), 0, 0);
      const formatted = formatExportDate(pickerDate);
      const valueInput = activeDateGroup?.querySelector("[data-audit-export-datetime-value]");
      const displayInput = activeDateGroup?.querySelector("[data-audit-export-display]");
      if (valueInput) {
        valueInput.value = formatted;
      }
      if (displayInput) {
        displayInput.value = formatted;
      }
      closeDatePicker();
    }
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      close();
    }
  });
};

const initScanActions = (root = document) => {
  root.querySelectorAll("[data-scan-action]").forEach((form) => {
    if (form.dataset.initialized === "true") {
      return;
    }

    form.dataset.initialized = "true";
    const scanButton = form.querySelector("[data-scan-button]");
    const scanButtonLabel = form.querySelector("[data-scan-button-label]");
    const scanSpinner = form.querySelector("[data-scan-spinner]");
    const scanStatusUrl = form.dataset.scanStatusUrl;
    const scanPollMs = Number.parseInt(form.dataset.scanPollMs || "5000", 10);
    let scanWasActive = scanButton ? scanButton.disabled : false;

    const setScanButtonState = (active, label) => {
      if (!scanButton || !scanButtonLabel) {
        return;
      }

      scanButton.disabled = Boolean(active);
      scanButton.classList.toggle("loading", Boolean(active));
      scanButtonLabel.textContent = label || (active ? "Scanning" : "Start scan");
      if (scanSpinner) {
        scanSpinner.hidden = !active;
      }
    };

    const loadScanStatus = async () => {
      if (!scanStatusUrl) {
        return;
      }

      try {
        const response = await fetch(scanStatusUrl, {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        if (scanWasActive && !data.active) {
          loadSoftNavigation(new URL(window.location.href), { push: false });
          return;
        }
        scanWasActive = Boolean(data.active);
        setScanButtonState(data.active, data.button_label);
      } catch (_error) {
        // The current button state remains usable if the status poll fails.
      }
    };

    form.addEventListener("submit", () => {
      scanWasActive = true;
      setScanButtonState(true, "Scan queued");
    });

    const intervalId = window.setInterval(
      () => {
        if (document.visibilityState !== "hidden") {
          loadScanStatus();
        }
      },
      Number.isFinite(scanPollMs) ? scanPollMs : 5000
    );
    registerPageCleanup(() => window.clearInterval(intervalId));
  });
};

const FILE_ACTION_META = {
  move: { action: "file.moved", name: "Move file" },
  copy: { action: "file.copied", name: "Copy file" },
  trash: { action: "file.trashed", name: "Move file to trash" },
  rename: { action: "file.renamed", name: "Rename file" },
  "new-folder": { action: "file.folder_created", name: "Create folder" },
  inflate: { action: "file.inflate_queued", name: "Inflate disk" },
};

const initScheduledRuns = (root = document) => {
  root.querySelectorAll("[data-scheduled-runs]").forEach((panel) => {
    if (panel.dataset.initialized === "true") {
      return;
    }

    panel.dataset.initialized = "true";
    const rows = panel.querySelector("[data-scheduled-run-rows]");
    const runsUrl = panel.dataset.scheduledRunsUrl || "";
    const pollMs = Number.parseInt(panel.dataset.scheduledRunsPollMs || "10000", 10);
    const hasRecentTaskbar = Boolean(document.querySelector("[data-recent-tasks]"));
    let loadingRuns = false;

    const runRowHtml = (run) => `
        <tr>
          <td>${escapeHtml(run.planned_for)}</td>
          <td>${escapeHtml(run.task)}</td>
          <td>${run.target_guest ? renderGuestLabel(run.target_guest) : escapeHtml(run.target)}</td>
          <td><span class="badge ${escapeHtml(run.status_class)}">${escapeHtml(run.status)}</span></td>
          <td>${escapeHtml(run.outcome)}</td>
          <td>${escapeHtml(run.started_at)}</td>
          <td>${escapeHtml(run.finished_at)}</td>
          <td>${escapeHtml(run.node)}</td>
          <td>${escapeHtml(run.message)}</td>
        </tr>
      `;

    const renderRuns = (runs) => {
      if (!rows) {
        return;
      }
      if (!runs.length) {
        rows.innerHTML = '<tr><td colspan="9" class="empty-state">No scheduled task runs yet.</td></tr>';
        return;
      }
      rows.innerHTML = runs.map(runRowHtml).join("");
    };

    const loadRuns = async () => {
      if (!runsUrl || loadingRuns) {
        return;
      }

      loadingRuns = true;
      try {
        const response = await fetch(new URL(runsUrl, window.location.origin), {
          headers: {
            Accept: "application/json",
          },
        });
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        renderRuns(data.runs || []);
      } catch (_error) {
        // Latest runs refresh is best effort; the server-rendered rows remain usable.
      } finally {
        loadingRuns = false;
      }
    };

    const refreshWithRecentTasks = () => {
      if (document.visibilityState !== "hidden") {
        loadRuns();
      }
    };
    window.addEventListener(recentTasksRefreshEvent, refreshWithRecentTasks);
    registerPageCleanup(() => window.removeEventListener(recentTasksRefreshEvent, refreshWithRecentTasks));

    if (!hasRecentTaskbar) {
      loadRuns();
      const intervalId = window.setInterval(refreshWithRecentTasks, Number.isFinite(pollMs) ? pollMs : 10000);
      registerPageCleanup(() => window.clearInterval(intervalId));
    }
  });
};

const vmOverviewRows = (overview) => Array.from(overview.querySelectorAll("[data-vm-overview-row]"));

const visibleVmOverviewRows = (overview) => vmOverviewRows(overview).filter((row) => !row.hidden);

const selectedVmOverviewRows = (overview) =>
  vmOverviewRows(overview).filter((row) => row.querySelector("[data-vm-select]")?.checked);

const initConfirmForms = (root) => {
  root.querySelectorAll("form[data-confirm]:not([data-guest-action-form])").forEach((form) => {
    if (form.dataset.confirmBound) return;
    form.dataset.confirmBound = "1";
    form.addEventListener("submit", async (event) => {
      if (form.dataset.confirmed === "true") {
        delete form.dataset.confirmed;
        return;
      }
      event.preventDefault();
      const confirmed = await openConfirmDialog({
        title: form.dataset.confirmTitle || "Confirm action",
        body: `<p>${escapeHtml(form.dataset.confirm || "Continue?")}</p>`,
        confirmLabel: form.dataset.confirmLabel || "Confirm",
        danger: form.dataset.confirmDanger === "true",
      });
      if (!confirmed) return;
      form.dataset.confirmed = "true";
      form.requestSubmit(event.submitter || undefined);
    });
  });
};

const initScheduledTaskForms = (root) => {
  root.querySelectorAll("[data-scheduled-task-form]").forEach((form) => {
    if (form.dataset.initialized === "true") return;
    form.dataset.initialized = "true";

    const targetSelect = form.querySelector("[data-scheduled-target]");
    const targetNode = form.querySelector("[data-scheduled-target-node]");
    const recurrenceKind = form.querySelector("[data-recurrence-kind]");
    const recurrenceFields = Array.from(form.querySelectorAll("[data-recurrence-field]"));
    const previewExpression = form.querySelector("[data-schedule-preview-expression]");
    const previewTime = form.querySelector("[data-schedule-preview-time]");
    const previewList = form.querySelector("[data-schedule-preview-list]");
    const calendarMonth = form.querySelector("[data-schedule-calendar-month]");
    const calendarGrid = form.querySelector("[data-schedule-calendar-grid]");
    const calendarPrev = form.querySelector("[data-schedule-calendar-prev]");
    const calendarNext = form.querySelector("[data-schedule-calendar-next]");
    const monthLabels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const ordinalNumbers = { first: 1, second: 2, third: 3, fourth: 4, fifth: 5 };
    let calendarOffset = 0;

    const enabledForRecurrence = (field, kind) => {
      const fieldKind = field.dataset.recurrenceField;
      if (fieldKind === "date") return kind === "once";
      if (fieldKind === "time") return true;
      if (fieldKind === "day") return kind === "monthly_day";
      if (fieldKind === "weekdays") return kind === "weekly" || kind === "monthly_ordinal";
      if (fieldKind === "ordinals") return kind === "monthly_ordinal";
      if (fieldKind === "months") return kind !== "once";
      if (fieldKind === "catch-up") return kind !== "once";
      return true;
    };

    const valueFor = (name) => form.querySelector(`[name="${name}"]`)?.value || "";
    const checkedValues = (name) =>
      Array.from(form.querySelectorAll(`[name="${name}"]:checked`)).map((input) => input.value);
    const pad = (value) => String(value).padStart(2, "0");
    const parsedNumber = (value, fallback, min, max) => {
      const parsed = Number.parseInt(value, 10);
      if (Number.isNaN(parsed)) return fallback;
      return Math.min(max, Math.max(min, parsed));
    };
    const runHour = () => parsedNumber(valueFor("run_hour"), 0, 0, 23);
    const runMinute = () => parsedNumber(valueFor("run_minute"), 0, 0, 59);
    const selectedMonths = () =>
      checkedValues("months")
        .map((value) => Number.parseInt(value, 10))
        .filter((value) => !Number.isNaN(value));
    const selectedWeekdays = () =>
      checkedValues("weekdays")
        .map((value) => Number.parseInt(value, 10))
        .filter((value) => !Number.isNaN(value));
    const selectedOrdinals = () => checkedValues("ordinals");
    const selectedDaysOfMonth = () =>
      valueFor("days_of_month")
        .split(",")
        .map((value) => Number.parseInt(value.trim(), 10))
        .filter((value) => !Number.isNaN(value) && value >= 1 && value <= 31);
    const localWeekday = (date) => (date.getDay() + 6) % 7;
    const formatTime = () => `${pad(runHour())}:${pad(runMinute())}`;
    const formatDateTime = (date) => {
      return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:00`;
    };
    const sameDayKey = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;

    const nthWeekdayOfMonth = (year, month, weekday, ordinal) => {
      if (ordinal === "last") {
        const date = new Date(year, month + 1, 0, runHour(), runMinute(), 0, 0);
        while (localWeekday(date) !== weekday) {
          date.setDate(date.getDate() - 1);
        }
        return date;
      }
      const ordinalNumber = ordinalNumbers[ordinal];
      if (!ordinalNumber) return null;
      const date = new Date(year, month, 1, runHour(), runMinute(), 0, 0);
      const offset = (weekday - localWeekday(date) + 7) % 7;
      date.setDate(1 + offset + (ordinalNumber - 1) * 7);
      return date.getMonth() === month ? date : null;
    };

    const addOccurrence = (occurrences, seen, date, now, months) => {
      if (!date || date <= now) return;
      if (months.length && !months.includes(date.getMonth() + 1)) return;
      const key = date.getTime();
      if (seen.has(key)) return;
      seen.add(key);
      occurrences.push(date);
    };

    const computeOccurrences = (limit = 80) => {
      const kind = recurrenceKind?.value || "once";
      const now = new Date();
      const hour = runHour();
      const minute = runMinute();
      const occurrences = [];
      const seen = new Set();
      const months = kind === "once" ? [] : selectedMonths();

      if (kind === "once") {
        const dateValue = valueFor("run_date");
        if (dateValue) {
          const date = new Date(`${dateValue}T${pad(hour)}:${pad(minute)}:00`);
          addOccurrence(occurrences, seen, date, new Date(0), months);
        }
        return occurrences;
      }

      if (!months.length) return [];

      if (kind === "daily" || kind === "weekly") {
        const weekdays = kind === "weekly" ? selectedWeekdays() : [];
        const cursor = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, minute, 0, 0);
        for (let i = 0; i < 1096 && occurrences.length < limit; i += 1) {
          if (kind === "daily" || weekdays.includes(localWeekday(cursor))) {
            addOccurrence(occurrences, seen, new Date(cursor), now, months);
          }
          cursor.setDate(cursor.getDate() + 1);
        }
      } else if (kind === "monthly_day") {
        const days = selectedDaysOfMonth();
        for (let offset = 0; offset < 84 && occurrences.length < limit; offset += 1) {
          const monthCursor = new Date(now.getFullYear(), now.getMonth() + offset, 1, hour, minute, 0, 0);
          days.forEach((day) => {
            const date = new Date(monthCursor.getFullYear(), monthCursor.getMonth(), day, hour, minute, 0, 0);
            if (date.getMonth() === monthCursor.getMonth()) {
              addOccurrence(occurrences, seen, date, now, months);
            }
          });
        }
      } else if (kind === "monthly_ordinal") {
        const weekdays = selectedWeekdays();
        const ordinals = selectedOrdinals();
        for (let offset = 0; offset < 84 && occurrences.length < limit; offset += 1) {
          const monthCursor = new Date(now.getFullYear(), now.getMonth() + offset, 1, hour, minute, 0, 0);
          ordinals.forEach((ordinal) => {
            weekdays.forEach((weekday) => {
              addOccurrence(
                occurrences,
                seen,
                nthWeekdayOfMonth(monthCursor.getFullYear(), monthCursor.getMonth(), weekday, ordinal),
                now,
                months
              );
            });
          });
        }
      }

      return occurrences.sort((a, b) => a.getTime() - b.getTime()).slice(0, limit);
    };

    const renderCalendar = (occurrences) => {
      if (!calendarMonth || !calendarGrid) return;
      const today = new Date();
      const monthDate = new Date(today.getFullYear(), today.getMonth() + calendarOffset, 1);
      calendarMonth.textContent = `${monthLabels[monthDate.getMonth()]} ${monthDate.getFullYear()}`;
      const runDays = new Set(
        occurrences
          .filter((date) => date.getFullYear() === monthDate.getFullYear() && date.getMonth() === monthDate.getMonth())
          .map((date) => sameDayKey(date))
      );
      const firstOffset = localWeekday(monthDate);
      const startDate = new Date(monthDate.getFullYear(), monthDate.getMonth(), 1 - firstOffset);
      calendarGrid.innerHTML = "";
      for (let index = 0; index < 42; index += 1) {
        const date = new Date(startDate.getFullYear(), startDate.getMonth(), startDate.getDate() + index);
        const day = document.createElement("span");
        day.className = "scheduled-calendar-day";
        day.textContent = String(date.getDate());
        if (date.getMonth() === monthDate.getMonth()) day.classList.add("in-month");
        if (runDays.has(sameDayKey(date))) day.classList.add("has-run");
        if (sameDayKey(date) === sameDayKey(today)) day.classList.add("today");
        calendarGrid.appendChild(day);
      }
    };

    const renderPreview = () => {
      const kind = recurrenceKind?.value || "once";
      const occurrences = computeOccurrences();
      const labels = {
        once: "Once",
        daily: "Daily",
        weekly: "Weekly",
        monthly_day: "Monthly by date",
        monthly_ordinal: "Monthly by weekday",
      };
      if (previewExpression) previewExpression.textContent = labels[kind] || "Custom";
      if (previewTime) previewTime.textContent = `At ${formatTime()}`;
      if (previewList) {
        previewList.innerHTML = "";
        occurrences.slice(0, 10).forEach((date) => {
          const item = document.createElement("li");
          item.textContent = formatDateTime(date);
          previewList.appendChild(item);
        });
        if (!previewList.children.length) {
          const item = document.createElement("li");
          item.textContent = "No matching runs";
          previewList.appendChild(item);
        }
      }
      renderCalendar(occurrences);
    };

    const updateTargetNode = () => {
      if (!targetSelect || !targetNode) return;
      const selectedOption = targetSelect.selectedOptions[0];
      targetNode.value = selectedOption?.dataset.node || "-";
    };

    const update = () => {
      updateTargetNode();
      const kind = recurrenceKind?.value || "once";
      recurrenceFields.forEach((field) => {
        const enabled = enabledForRecurrence(field, kind);
        field.classList.toggle("scheduled-field-disabled", !enabled);
        field.querySelectorAll("input:not([type='hidden']), select, textarea").forEach((control) => {
          control.disabled = !enabled;
        });
      });
      renderPreview();
    };

    calendarPrev?.addEventListener("click", () => {
      calendarOffset -= 1;
      renderPreview();
    });
    calendarNext?.addEventListener("click", () => {
      calendarOffset += 1;
      renderPreview();
    });
    form.addEventListener("input", update);
    form.addEventListener("change", update);
    recurrenceKind?.addEventListener("change", update);
    update();
  });
};

export {
  FILE_ACTION_META,
  initAuditExportDialog,
  initAutoSubmitForms,
  initConfirmForms,
  initScanActions,
  initScheduledRuns,
  initScheduledTaskForms,
  selectedVmOverviewRows,
  visibleVmOverviewRows,
  vmOverviewRows,
};
