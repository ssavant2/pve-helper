import { loadSoftNavigation } from "./navigation.js";
import { groupRowsBySubtree, registerPageCleanup } from "./shell.js";
import { syncVmOverviewSelection } from "./vm-overview.js";

const initSpaceCharts = (root) => {
  root.querySelectorAll("[data-space-chart]").forEach((svg) => {
    if (svg.dataset.chartRendered) return;
    svg.dataset.chartRendered = "1";

    let raw;
    try {
      raw = JSON.parse(svg.dataset.chartData || "[]");
    } catch (_) {
      return;
    }
    if (!raw.length) return;

    const rect = svg.getBoundingClientRect();
    const W = rect.width || 600;
    const H = 220;
    const PL = 98,
      PR = 18,
      PT = 34,
      PB = 48;
    const pW = W - PL - PR;
    const pH = H - PT - PB;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

    let maxB = 0;
    raw.forEach((d) => {
      if (d.total_bytes > maxB) maxB = d.total_bytes;
    });
    if (!maxB) return;

    const ts = raw.map((d) => new Date(d.timestamp).getTime());
    const sevenDaysMs = 7 * 24 * 60 * 60 * 1000;
    const chartEnd = ts[ts.length - 1];
    const chartStart = Math.min(ts[0], chartEnd - sevenDaysMs);
    const chartRange = chartEnd - chartStart || 1;
    const xOf = (t) => PL + ((t - chartStart) / chartRange) * pW;
    const yOf = (b) => PT + pH - (b / maxB) * pH;
    const colors = {
      used: "#2f8de4",
      free: "#7c4d9e",
      total: "#35d04f",
      grid: "rgba(179, 202, 219, 0.28)",
      label: "#cfe7ff",
    };
    const fmt = (b) => {
      if (b >= 549755813888) return `${(b / 1099511627776).toFixed(1)} TB`;
      if (b >= 1073741824) return `${(b / 1073741824).toFixed(1)} GB`;
      if (b >= 1048576) return `${(b / 1048576).toFixed(1)} MB`;
      return `${(b / 1024).toFixed(1)} KB`;
    };
    const pad2 = (n) => String(n).padStart(2, "0");
    const fmtDate = (d) => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
    const fmtClock = (d) => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
    const ns = "http://www.w3.org/2000/svg";
    const el = (tag, a) => {
      const e = document.createElementNS(ns, tag);
      const styleProps = ["fill", "stroke", "opacity"];
      let styleStr = "";
      for (const k in a) {
        if (styleProps.includes(k) || k === "font-size" || k === "font-weight") {
          styleStr += `${k}:${a[k]};`;
        } else {
          e.setAttribute(k, a[k]);
        }
      }
      if (styleStr) e.setAttribute("style", styleStr);
      return e;
    };
    const tx = (tag, a, t) => {
      const e = el(tag, a);
      e.textContent = t;
      return e;
    };

    svg.appendChild(el("rect", { x: PL, y: PT, width: pW, height: pH, fill: "rgba(10, 20, 30, 0.18)" }));

    for (let i = 0; i <= 5; i++) {
      const percent = 100 - i * 20;
      const yP = PT + (pH / 5) * i;
      const v = maxB * (percent / 100);
      svg.appendChild(el("line", { x1: PL, y1: yP, x2: W - PR, y2: yP, stroke: colors.grid, "stroke-width": "1" }));
      svg.appendChild(
        tx("text", { x: PL - 42, y: yP + 4, "text-anchor": "end", fill: colors.label, "font-size": "11" }, fmt(v))
      );
      svg.appendChild(
        tx(
          "text",
          { x: PL - 6, y: yP + 4, "text-anchor": "end", fill: "var(--muted)", "font-size": "10" },
          `${percent}%`
        )
      );
    }

    const series =
      raw.length === 1
        ? [
            { ...raw[0], chart_ts: chartStart },
            { ...raw[0], chart_ts: chartEnd },
          ]
        : raw.map((d) => ({ ...d, chart_ts: new Date(d.timestamp).getTime() }));
    const seriesTs = series.map((d) => d.chart_ts);
    const linePath = (key) => {
      let path = `M ${xOf(seriesTs[0])} ${yOf(series[0][key])}`;
      series.forEach((d, i) => {
        path += ` L ${xOf(seriesTs[i])} ${yOf(d[key])}`;
      });
      return path;
    };
    const areaToBottom = (key) => {
      let path = linePath(key);
      path += ` L ${xOf(seriesTs[seriesTs.length - 1])} ${PT + pH} L ${xOf(seriesTs[0])} ${PT + pH} Z`;
      return path;
    };
    const areaBetween = (upperKey, lowerKey) => {
      let path = linePath(upperKey);
      for (let i = series.length - 1; i >= 0; i--) {
        path += ` L ${xOf(seriesTs[i])} ${yOf(series[i][lowerKey])}`;
      }
      return `${path} Z`;
    };

    svg.appendChild(el("path", { d: areaToBottom("used_bytes"), fill: colors.used, opacity: "0.72" }));
    svg.appendChild(el("path", { d: areaBetween("total_bytes", "used_bytes"), fill: colors.free, opacity: "0.62" }));
    svg.appendChild(
      el("path", { d: linePath("used_bytes"), fill: "none", stroke: colors.used, "stroke-width": "2.5" })
    );
    svg.appendChild(
      el("path", { d: linePath("total_bytes"), fill: "none", stroke: colors.total, "stroke-width": "2.5" })
    );

    // Data points
    raw.forEach((d, i) => {
      svg.appendChild(
        el("circle", {
          cx: xOf(ts[i]),
          cy: yOf(d.used_bytes),
          r: "3.5",
          fill: colors.used,
          stroke: "var(--surface)",
          "stroke-width": "1",
        })
      );
    });

    // Time labels
    [
      [chartStart, "start"],
      [chartStart + chartRange / 2, "middle"],
      [chartEnd, "end"],
    ].forEach(([labelTs, anchor]) => {
      const dt = new Date(labelTs);
      const textAnchor = anchor === "start" ? "start" : anchor === "end" ? "end" : "middle";
      const x = anchor === "start" ? PL : anchor === "end" ? W - PR : xOf(labelTs);
      svg.appendChild(
        tx("text", { x, y: H - 20, "text-anchor": textAnchor, fill: "var(--muted)", "font-size": "10" }, fmtDate(dt))
      );
      svg.appendChild(
        tx("text", { x, y: H - 7, "text-anchor": textAnchor, fill: "var(--muted)", "font-size": "10" }, fmtClock(dt))
      );
    });

    // Legend
    const legend = [
      ["Used", colors.used, PL + 8],
      ["Free", colors.free, PL + 78],
      ["Total", colors.total, PL + 142],
    ];
    legend.forEach(([label, color, x]) => {
      svg.appendChild(el("rect", { x, y: PT - 24, width: "10", height: "10", fill: color }));
      svg.appendChild(tx("text", { x: x + 16, y: PT - 15, fill: "var(--muted)", "font-size": "11" }, label));
    });

    // Now value label
    const last = raw[raw.length - 1];
    const lastX = xOf(ts[ts.length - 1]);
    const lastLabelNearRight = lastX > W - PR - 72;
    svg.appendChild(
      tx(
        "text",
        {
          x: lastLabelNearRight ? W - PR : Math.max(PL, lastX),
          y: Math.max(PT + 11, yOf(last.used_bytes) - 8),
          "text-anchor": lastLabelNearRight ? "end" : "middle",
          fill: "#d7ecff",
          "font-size": "10",
          "font-weight": "600",
        },
        `${fmt(last.used_bytes)} used`
      )
    );
  });
};

const initTableFilters = (root) => {
  root.querySelectorAll("[data-table-filter]").forEach((input) => {
    if (input.dataset.filterBound) return;
    input.dataset.filterBound = "1";
    const selector = input.dataset.tableFilter || "";
    const table = selector
      ? document.querySelector(selector)
      : input.closest(".panel")?.querySelector("[data-filterable-table]");
    if (!table) return;
    const applyFilter = () => {
      const q = input.value.toLowerCase().trim();
      table.querySelectorAll("tbody tr[data-filter-text]").forEach((row) => {
        row.hidden = q && !row.dataset.filterText.includes(q);
      });
      const overview = table.closest("[data-vm-overview]");
      if (overview) {
        syncVmOverviewSelection(overview);
      }
    };
    input.addEventListener("input", applyFilter);
    const query = new URLSearchParams(window.location.search).get("q");
    if (query && !input.value) {
      input.value = query;
      applyFilter();
    }
  });
};

const initColumnPickers = (root) => {
  root.querySelectorAll("[data-column-picker]").forEach((picker) => {
    if (picker.dataset.initialized === "true") return;
    picker.dataset.initialized = "true";

    const tableName = picker.dataset.columnPicker || "";
    const table = document.querySelector(`[data-column-table="${CSS.escape(tableName)}"]`);
    if (!table) return;

    const storageKey = `pve-helper-columns-${tableName}`;
    const toggles = Array.from(picker.querySelectorAll("[data-column-toggle]"));
    const panel = picker.querySelector(".column-picker-panel");
    const defaultState = {};
    toggles.forEach((toggle) => {
      defaultState[toggle.dataset.columnToggle] = toggle.checked;
    });
    document.addEventListener("click", (event) => {
      if (picker.open && !picker.contains(event.target)) {
        picker.open = false;
      }
    });

    let state = { ...defaultState };
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
      if (stored && typeof stored === "object") {
        state = { ...state, ...stored };
      }
    } catch (_error) {
      state = { ...defaultState };
    }

    // Visibility-only picker: list the columns alphabetically for easy
    // scanning. Column *reordering* lives in the table headers
    // (initResizableColumns), so the picker no longer drags/orders.
    if (panel) {
      const labelText = (toggle) => (toggle.closest("label")?.textContent || "").trim();
      toggles
        .slice()
        .sort((left, right) => labelText(left).localeCompare(labelText(right)))
        .forEach((toggle) => {
          const label = toggle.closest("label");
          if (label) {
            panel.appendChild(label);
          }
        });
    }

    const apply = () => {
      toggles.forEach((toggle) => {
        const column = toggle.dataset.columnToggle;
        if (!column) return;
        if (!toggle.disabled) {
          toggle.checked = state[column] !== false;
        }
        const visible = toggle.disabled || toggle.checked;
        table.querySelectorAll(`[data-column="${CSS.escape(column)}"]`).forEach((cell) => {
          cell.hidden = !visible;
        });
      });
      table.dispatchEvent(new CustomEvent("pve-helper-columns-changed"));
    };

    toggles.forEach((toggle) => {
      toggle.addEventListener("change", () => {
        state[toggle.dataset.columnToggle] = toggle.checked;
        try {
          localStorage.setItem(storageKey, JSON.stringify(state));
        } catch (_error) {
          // Column preferences are optional.
        }
        apply();
      });
    });

    apply();
  });
};

const initResizableColumns = (root) => {
  root.querySelectorAll("[data-resizable-columns][data-column-table]").forEach((table) => {
    if (table.dataset.resizableColumnsInitialized === "true") return;
    table.dataset.resizableColumnsInitialized = "true";

    const tableName = table.dataset.columnTable || table.id || "table";
    const allowColumnReorder = true;
    const storageKey = `pve-helper-column-widths-${tableName}`;
    const orderStorageKey = `pve-helper-columns-${tableName}-order`;
    let storedWidths = {};
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
      if (stored && typeof stored === "object") {
        storedWidths = stored;
      }
    } catch (_error) {
      storedWidths = {};
    }

    let measureCanvas = null;
    const textWidth = (text, element) => {
      measureCanvas ||= document.createElement("canvas");
      const context = measureCanvas.getContext("2d");
      const style = window.getComputedStyle(element);
      context.font = `${style.fontStyle} ${style.fontVariant} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
      return context.measureText(
        String(text || "")
          .replace(/\s+/g, " ")
          .trim()
      ).width;
    };

    const numericStyle = (element, property) => {
      const value = Number.parseFloat(window.getComputedStyle(element)[property]);
      return Number.isFinite(value) ? value : 0;
    };

    const columnCells = (column) => Array.from(table.querySelectorAll(`[data-column="${CSS.escape(column)}"]`));

    const minColumnWidth = (column) => {
      if (column === "name") return 150;
      if (column === "cpu") return 56;
      if (["cpus", "nics", "disks"].includes(column)) return 52;
      if (column === "vmid") return 64;
      if (column === "type") return 68;
      if (["status", "outcome"].includes(column)) return 82;
      if (["initiator", "user", "module"].includes(column)) return 76;
      if (["queued", "started", "finished", "time"].includes(column)) return 118;
      return 90;
    };

    const saveWidths = () => {
      try {
        localStorage.setItem(storageKey, JSON.stringify(storedWidths));
      } catch (_error) {
        // Column width preferences are optional.
      }
    };

    const allColumns = () =>
      Array.from(table.tHead?.rows?.[0]?.querySelectorAll("th[data-column]") || [])
        .map((cell) => cell.dataset.column)
        .filter(Boolean);

    let columnOrder = allColumns();
    if (allowColumnReorder) {
      try {
        const storedOrder = JSON.parse(localStorage.getItem(orderStorageKey) || "[]");
        if (Array.isArray(storedOrder)) {
          const known = new Set(columnOrder);
          columnOrder = [
            ...storedOrder.filter((column) => known.has(column)),
            ...columnOrder.filter((column) => !storedOrder.includes(column)),
          ];
        }
      } catch (_error) {
        columnOrder = allColumns();
      }
    }

    const normalizeColumnOrder = (nextOrder) => {
      const defaultOrder = allColumns();
      const known = new Set(defaultOrder);
      const normalized = [
        ...nextOrder.filter((column) => known.has(column)),
        ...defaultOrder.filter((column) => !nextOrder.includes(column)),
      ];
      if (tableName === "vm-overview" && normalized.includes("name")) {
        return ["name", ...normalized.filter((column) => column !== "name")];
      }
      return normalized;
    };

    const saveColumnOrder = () => {
      try {
        localStorage.setItem(orderStorageKey, JSON.stringify(columnOrder));
      } catch (_error) {
        // Column order preferences are optional.
      }
    };

    const applyColumnOrder = () => {
      columnOrder = normalizeColumnOrder(columnOrder);
      Array.from(table.rows).forEach((row) => {
        const cells = Array.from(row.children);
        const fixedCells = cells.filter((cell) => !cell.dataset.column);
        const cellsByColumn = new Map();
        cells
          .filter((cell) => cell.dataset.column)
          .forEach((cell) => {
            cellsByColumn.set(cell.dataset.column, cell);
          });
        fixedCells.forEach((cell) => {
          row.appendChild(cell);
        });
        columnOrder.forEach((column) => {
          const cell = cellsByColumn.get(column);
          if (cell) {
            row.appendChild(cell);
          }
        });
      });
    };

    const applyNewColumnOrder = (nextOrder) => {
      columnOrder = normalizeColumnOrder(nextOrder);
      saveColumnOrder();
      applyColumnOrder();
      table.dispatchEvent(new CustomEvent("pve-helper-column-order-changed", { detail: { order: columnOrder } }));
      table.dispatchEvent(new CustomEvent("pve-helper-columns-changed"));
    };

    const visibleHeaderCells = () => Array.from(table.tHead?.rows?.[0]?.children || []).filter((cell) => !cell.hidden);

    const defaultColumnWidths = {
      name: 260,
      state: 120,
      provisioned: 130,
      used: 110,
      cpu: 74,
      "host-mem": 110,
      "active-mem": 130,
      "guest-os": 200,
      agent: 120,
      node: 100,
      "has-snapshot": 110,
      vmid: 70,
      type: 70,
      "memory-size": 110,
      cpus: 58,
      nics: 58,
      disks: 58,
      uptime: 100,
      ip: 170,
      mac: 170,
      storage: 170,
      tags: 170,
      "task-name": 340,
      target: 260,
      status: 150,
      details: 340,
      initiator: 120,
      queued: 140,
      started: 155,
      finished: 155,
      server: 95,
      time: 160,
      module: 95,
      user: 130,
      source: 130,
      action: 220,
      object: 260,
      outcome: 105,
    };

    const baseCellWidth = (cell) => {
      if (cell.classList.contains("vm-select-column")) return 36;
      const column = cell.dataset.column || "";
      return Number(storedWidths[column]) || defaultColumnWidths[column] || minColumnWidth(column);
    };

    const updateTableWidth = () => {
      const headerCells = visibleHeaderCells();
      const total = headerCells.reduce((sum, cell) => sum + baseCellWidth(cell), 0);
      const scroll = table.closest(".data-table-scroll, .task-table-wrap") || table.parentElement;
      const available = scroll?.clientWidth || table.parentElement?.clientWidth || 0;
      const width = Math.max(Math.ceil(available), 1);
      const stretchCell = headerCells.filter((cell) => cell.dataset.column).at(-1);
      const stretchColumn = stretchCell?.dataset.column || "";
      const slack = stretchColumn ? width - total : 0;
      headerCells.forEach((cell) => {
        const column = cell.dataset.column || "";
        const renderedWidth = Math.max(
          column && column === stretchColumn ? minColumnWidth(column) : baseCellWidth(cell),
          baseCellWidth(cell) + (column && column === stretchColumn ? slack : 0)
        );
        const targets = column ? columnCells(column) : [cell];
        targets.forEach((target) => {
          target.style.width = `${renderedWidth}px`;
          target.style.minWidth = "0";
        });
      });
      table.style.width = `${width}px`;
      table.style.minWidth = `${width}px`;
    };

    const setColumnWidth = (column, width, persist = true) => {
      const normalized = Math.max(minColumnWidth(column), Math.round(width));
      columnCells(column).forEach((cell) => {
        cell.style.width = `${normalized}px`;
        cell.style.minWidth = `${normalized}px`;
      });
      storedWidths[column] = normalized;
      if (persist) {
        saveWidths();
      }
      updateTableWidth();
    };

    const autoFitWidth = (header) => {
      const column = header.dataset.column;
      if (!column) return header.getBoundingClientRect().width;
      const headerPadding = numericStyle(header, "paddingLeft") + numericStyle(header, "paddingRight");
      const headerControlAllowance = 10;
      let width =
        textWidth(header.dataset.columnLabel || header.textContent, header) + headerPadding + headerControlAllowance;
      table.querySelectorAll(`tbody tr:not([hidden]) [data-column="${CSS.escape(column)}"]`).forEach((cell) => {
        if (cell.hidden) return;
        const padding = numericStyle(cell, "paddingLeft") + numericStyle(cell, "paddingRight");
        const iconAllowance = column === "name" ? 28 : 0;
        width = Math.max(width, textWidth(cell.textContent, cell) + padding + iconAllowance + 4);
      });
      return Math.ceil(width);
    };

    table.querySelectorAll("thead th[data-column]").forEach((header) => {
      const column = header.dataset.column;
      if (!column) return;
      header.dataset.columnLabel ||= header.textContent.trim();
      if (allowColumnReorder && !(tableName === "vm-overview" && column === "name")) {
        header.draggable = true;
        header.title =
          header.title ||
          (table.matches("[data-sortable-table]") ? "Drag to reorder. Click to sort." : "Drag to reorder.");
      }
      const storedWidth = Number(storedWidths[column]);
      if (Number.isFinite(storedWidth) && storedWidth > 0) {
        setColumnWidth(column, storedWidth, false);
      }
      if (header.querySelector("[data-column-resize-handle]")) return;
      const handle = document.createElement("span");
      handle.className = "column-resize-handle";
      handle.dataset.columnResizeHandle = "true";
      handle.title = "Drag to resize. Double-click to fit.";
      handle.setAttribute("aria-hidden", "true");
      header.appendChild(handle);

      handle.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
      });

      handle.addEventListener("dblclick", (event) => {
        event.preventDefault();
        event.stopPropagation();
        setColumnWidth(column, autoFitWidth(header));
      });

      handle.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        const startX = event.clientX;
        const startWidth = header.getBoundingClientRect().width;
        table.classList.add("column-resizing");
        handle.classList.add("active");

        const onPointerMove = (moveEvent) => {
          moveEvent.preventDefault();
          setColumnWidth(column, startWidth + moveEvent.clientX - startX, false);
        };

        const onPointerUp = () => {
          table.classList.remove("column-resizing");
          handle.classList.remove("active");
          saveWidths();
          document.removeEventListener("pointermove", onPointerMove);
          document.removeEventListener("pointerup", onPointerUp);
          document.removeEventListener("pointercancel", onPointerUp);
        };

        document.addEventListener("pointermove", onPointerMove);
        document.addEventListener("pointerup", onPointerUp);
        document.addEventListener("pointercancel", onPointerUp);
      });
    });

    let draggedHeaderColumn = "";
    table.addEventListener("dragstart", (event) => {
      if (!allowColumnReorder) return;
      const header = event.target.closest("thead th[data-column]");
      const column = header?.dataset.column || "";
      if (!header || !table.contains(header) || !column || (tableName === "vm-overview" && column === "name")) return;
      draggedHeaderColumn = column;
      header.classList.add("column-dragging");
      table.dataset.columnDragging = "true";
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", column);
    });

    table.addEventListener("dragover", (event) => {
      if (!allowColumnReorder) return;
      if (!draggedHeaderColumn) return;
      const header = event.target.closest("thead th[data-column]");
      const targetColumn = header?.dataset.column || "";
      if (!header || !table.contains(header) || header.hidden || !targetColumn || targetColumn === draggedHeaderColumn)
        return;
      if (tableName === "vm-overview" && targetColumn === "name") return;
      event.preventDefault();
      const rect = header.getBoundingClientRect();
      const after = event.clientX > rect.left + rect.width / 2;
      table.querySelectorAll("thead th.drag-over-before, thead th.drag-over-after").forEach((cell) => {
        cell.classList.remove("drag-over-before", "drag-over-after");
      });
      header.classList.toggle("drag-over-before", !after);
      header.classList.toggle("drag-over-after", after);
    });

    table.addEventListener("drop", (event) => {
      if (!allowColumnReorder) return;
      if (!draggedHeaderColumn) return;
      const header = event.target.closest("thead th[data-column]");
      const targetColumn = header?.dataset.column || "";
      if (!header || !table.contains(header) || header.hidden || !targetColumn || targetColumn === draggedHeaderColumn)
        return;
      if (tableName === "vm-overview" && targetColumn === "name") return;
      event.preventDefault();
      const after = header.classList.contains("drag-over-after");
      const visibleColumns = new Set(
        Array.from(table.querySelectorAll("thead th[data-column]:not([hidden])"))
          .map((cell) => cell.dataset.column)
          .filter(Boolean)
      );
      const visibleOrder = columnOrder.filter((column) => visibleColumns.has(column));
      const hiddenOrder = columnOrder.filter((column) => !visibleColumns.has(column));
      const nextVisibleOrder = visibleOrder.filter((column) => column !== draggedHeaderColumn);
      const targetIndex = nextVisibleOrder.indexOf(targetColumn);
      if (targetIndex < 0) return;
      nextVisibleOrder.splice(targetIndex + (after ? 1 : 0), 0, draggedHeaderColumn);
      const nextOrder = [...nextVisibleOrder, ...hiddenOrder.filter((column) => column !== draggedHeaderColumn)];
      table.dataset.suppressSortClick = "true";
      applyNewColumnOrder(nextOrder);
    });

    table.addEventListener("dragend", () => {
      if (!allowColumnReorder) return;
      draggedHeaderColumn = "";
      delete table.dataset.columnDragging;
      table
        .querySelectorAll("thead th.column-dragging, thead th.drag-over-before, thead th.drag-over-after")
        .forEach((cell) => {
          cell.classList.remove("column-dragging", "drag-over-before", "drag-over-after");
        });
      window.setTimeout(() => {
        delete table.dataset.suppressSortClick;
      }, 200);
    });

    table.addEventListener("pve-helper-columns-changed", updateTableWidth);
    window.addEventListener("resize", updateTableWidth);
    registerPageCleanup(() => window.removeEventListener("resize", updateTableWidth));
    applyColumnOrder();
    updateTableWidth();
  });
};

const initSortableTables = (root) => {
  root.querySelectorAll("[data-sortable-table]").forEach((table) => {
    if (table.dataset.sortableInitialized === "true") return;
    table.dataset.sortableInitialized = "true";

    const headers = Array.from(table.querySelectorAll("thead th[data-sort]"));
    const tableName =
      table.dataset.columnTable ||
      table.id ||
      `table-${Array.from(document.querySelectorAll("[data-sortable-table]")).indexOf(table)}`;
    const storageKey = `pve-helper-sort-${tableName}`;

    const readStoredSort = () => {
      try {
        const stored = JSON.parse(localStorage.getItem(storageKey) || "{}");
        if (stored && typeof stored === "object" && stored.column && stored.direction) {
          return stored;
        }
      } catch (_error) {
        // Sorting remains usable without localStorage.
      }
      return null;
    };

    const writeStoredSort = (column, direction) => {
      try {
        localStorage.setItem(storageKey, JSON.stringify({ column, direction }));
      } catch (_error) {
        // Sorting remains usable without localStorage.
      }
    };

    const sortByHeader = (header, direction, persist = true) => {
      const index = Array.from(header.parentElement?.children || []).indexOf(header);
      if (index < 0) return;
      headers.forEach((other) => {
        other.dataset.sortDirection = "";
        other.removeAttribute("aria-sort");
      });
      header.dataset.sortDirection = direction;
      header.setAttribute("aria-sort", direction === "asc" ? "ascending" : "descending");

      const numeric = header.dataset.sort === "number";
      const tbody = table.tBodies[0];
      const rows = Array.from(tbody.querySelectorAll("tr")).filter((row) => row.children.length > 1);
      rows.sort((a, b) => {
        const aCell = a.children[index];
        const bCell = b.children[index];
        const aRaw = aCell?.dataset.sortValue ?? aCell?.textContent ?? "";
        const bRaw = bCell?.dataset.sortValue ?? bCell?.textContent ?? "";
        const result = numeric
          ? Number(aRaw || 0) - Number(bRaw || 0)
          : String(aRaw).localeCompare(String(bRaw), undefined, { numeric: true, sensitivity: "base" });
        return direction === "asc" ? result : -result;
      });
      // Sorting the overview by name clumps linked clones under their parent
      // (children sort by the parent's name); every other column stays flat.
      const nameClump = tableName === "vm-overview" && (header.dataset.column || "") === "name";
      const finalRows = nameClump ? groupRowsBySubtree(rows) : rows;
      finalRows.forEach((row) => {
        tbody.appendChild(row);
      });
      if (persist) {
        writeStoredSort(header.dataset.column || header.textContent.trim(), direction);
      }
      const overview = table.closest("[data-vm-overview]");
      if (overview) {
        syncVmOverviewSelection(overview);
      }
    };

    table.pveHelperApplyStoredSort = () => {
      const stored = readStoredSort();
      if (!stored) return;
      const header = headers.find(
        (candidate) => (candidate.dataset.column || candidate.textContent.trim()) === stored.column
      );
      if (!header) return;
      sortByHeader(header, stored.direction === "desc" ? "desc" : "asc", false);
    };

    headers.forEach((header) => {
      header.tabIndex = 0;
      header.classList.add("sortable-heading");
      const sort = () => {
        if (table.dataset.columnDragging === "true" || table.dataset.suppressSortClick === "true") {
          delete table.dataset.suppressSortClick;
          return;
        }
        const direction = header.dataset.sortDirection === "asc" ? "desc" : "asc";
        sortByHeader(header, direction);
      };
      header.addEventListener("click", sort);
      header.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          sort();
        }
      });
    });
    table.pveHelperApplyStoredSort();
    // Default (no stored sort): the overview arrives name-ordered from the
    // server, so clump linked clones under their parent for the initial view.
    if (tableName === "vm-overview" && !readStoredSort()) {
      const tbody = table.tBodies[0];
      if (tbody) {
        const rows = Array.from(tbody.querySelectorAll("tr")).filter((row) => row.children.length > 1);
        groupRowsBySubtree(rows).forEach((row) => {
          tbody.appendChild(row);
        });
      }
    }
  });
};

const initGuestListFilter = (root = document) => {
  root.querySelectorAll("[data-guest-filter]").forEach((input) => {
    if (input.dataset.initialized === "true") {
      return;
    }
    input.dataset.initialized = "true";
    const pane = input.closest("[data-guest-pane]");
    const list = pane ? pane.querySelector("[data-guest-list]") : null;
    if (!list) {
      return;
    }
    const apply = () => {
      const query = input.value.trim().toLowerCase();
      const items = Array.from(list.querySelectorAll("[data-filter-text]"));
      let visibleCount = 0;
      items.forEach((item) => {
        const text = item.dataset.filterText || "";
        const hidden = query !== "" && !text.includes(query);
        item.hidden = hidden;
        if (!hidden && !item.classList.contains("cluster-hidden")) {
          visibleCount += 1;
        }
      });
      list.querySelectorAll("[data-guest-cluster-group]").forEach((header) => {
        const clusterKey = header.dataset.guestClusterGroup || "";
        header.hidden = !items.some(
          (item) =>
            (item.dataset.guestCluster || "") === clusterKey &&
            !item.hidden &&
            !item.classList.contains("cluster-hidden")
        );
      });
      const empty = list.querySelector("[data-guest-filter-empty]");
      if (empty) {
        empty.hidden = visibleCount > 0;
      }
    };
    input.addEventListener("input", apply);
    document.addEventListener("pve-helper:cluster-filter-changed", apply);
    registerPageCleanup(() => document.removeEventListener("pve-helper:cluster-filter-changed", apply));
    apply();
  });
};

const initSummaryCards = (root = document) => {
  const grid = root.querySelector("[data-summary-cards]");
  if (!grid || grid.dataset.initialized === "true") {
    return;
  }
  grid.dataset.initialized = "true";
  const orderKey = "pve-helper-vm-summary-order";
  const sizeKey = "pve-helper-vm-summary-card-sizes";
  const layoutKey = "pve-helper-vm-summary-layout-v2";
  // Hidden cards (via the "Show cards" picker) are display:none — exclude them
  // from layout/order/drag so the packing leaves no gap where they'd sit.
  const cardList = () =>
    Array.from(grid.querySelectorAll("[data-card-key]")).filter((card) => card.style.display !== "none");
  const autoCardList = () => cardList().filter((card) => card.dataset.cardSize === "auto");

  const savedSizes = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(sizeKey) || "{}");
      return saved && typeof saved === "object" ? saved : {};
    } catch (_error) {
      return {};
    }
  };

  const persistSizes = () => {
    const sizes = {};
    autoCardList().forEach((card) => {
      if (card.dataset.cardExpanded === "true") {
        sizes[card.dataset.cardKey] = "expanded";
      }
    });
    try {
      localStorage.setItem(sizeKey, JSON.stringify(sizes));
    } catch (_error) {
      // persistence is optional
    }
  };

  const syncSizeToggle = (card) => {
    const button = card.querySelector("[data-card-size-toggle]");
    if (!button) {
      return;
    }
    const expanded = card.dataset.cardExpanded === "true";
    const label = card.querySelector(".panel-heading h2")?.textContent?.trim() || "card";
    button.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} ${label}`);
    button.title = expanded ? "Collapse" : "Expand";
  };

  const cardSpan = (card) => (card.dataset.cardSize === "full" || card.dataset.cardExpanded === "true" ? 2 : 1);

  const gridMetrics = () => {
    const style = window.getComputedStyle(grid);
    const columns = style.gridTemplateColumns.split(" ").filter((value) => value && value !== "none").length || 1;
    const columnGap = Number.parseFloat(style.columnGap) || 0;
    const rowGap = Number.parseFloat(style.rowGap) || 0;
    const rect = grid.getBoundingClientRect();
    const columnWidth = (rect.width - columnGap * Math.max(0, columns - 1)) / columns;
    const rowHeight = Number.parseFloat(style.gridAutoRows) || columnWidth;
    return { columnGap, columns, columnWidth, rect, rowGap, rowHeight };
  };

  const cardGridPosition = (card, metrics) => {
    const rect = card.getBoundingClientRect();
    const rowStride = metrics.rowHeight + metrics.rowGap;
    const columnStride = metrics.columnWidth + metrics.columnGap;
    return {
      x: Math.max(
        0,
        Math.min(metrics.columns - 1, Math.round((rect.left - metrics.rect.left) / Math.max(1, columnStride)))
      ),
      y: Math.max(0, Math.round((rect.top - metrics.rect.top) / Math.max(1, rowStride))),
    };
  };

  const pointerGridPosition = (event, metrics) => {
    const columnStride = metrics.columnWidth + metrics.columnGap;
    const rowStride = metrics.rowHeight + metrics.rowGap;
    if (event.clientX < metrics.rect.left || event.clientX > metrics.rect.right || event.clientY < metrics.rect.top) {
      return null;
    }
    return {
      x: Math.max(
        0,
        Math.min(metrics.columns - 1, Math.floor((event.clientX - metrics.rect.left) / Math.max(1, columnStride)))
      ),
      y: Math.max(0, Math.floor((event.clientY - metrics.rect.top) / Math.max(1, rowStride))),
    };
  };

  const loadLayout = () => {
    try {
      const saved = JSON.parse(localStorage.getItem(layoutKey) || "{}");
      return saved && typeof saved === "object" ? saved : {};
    } catch (_error) {
      return {};
    }
  };

  const persistLayout = (layout) => {
    try {
      localStorage.setItem(layoutKey, JSON.stringify(layout));
    } catch (_error) {
      // persistence is optional
    }
  };

  const layoutFromDom = () => {
    const metrics = gridMetrics();
    const layout = {};
    cardList().forEach((card) => {
      layout[card.dataset.cardKey] = cardGridPosition(card, metrics);
    });
    return layout;
  };

  const hasCollision = (item, placed) =>
    placed.find((other) => item.x === other.x && item.y < other.y + other.span && item.y + item.span > other.y);

  const resolveLayout = (layout, preferredKey = "") => {
    const metrics = gridMetrics();
    const byKey = new Map(cardList().map((card) => [card.dataset.cardKey, card]));
    const domIndex = (key) => cardList().indexOf(byKey.get(key));
    // Cards with a saved slot keep it; cards without one (re-shown from the
    // picker, or brand new) are dropped into the first free slot afterwards
    // instead of piling onto (0,0).
    const positioned = [];
    const unpositioned = [];
    cardList().forEach((card) => {
      const key = card.dataset.cardKey;
      const saved = layout[key] || {};
      const item = { key, span: cardSpan(card) };
      if (Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
        item.x = Math.max(0, Math.min(metrics.columns - 1, saved.x));
        item.y = Math.max(0, saved.y);
        positioned.push(item);
      } else {
        unpositioned.push(item);
      }
    });
    positioned.sort((left, right) => {
      if (left.key === preferredKey) {
        return -1;
      }
      if (right.key === preferredKey) {
        return 1;
      }
      return left.y - right.y || left.x - right.x || domIndex(left.key) - domIndex(right.key);
    });
    const placed = [];
    positioned.forEach((item) => {
      let collision = hasCollision(item, placed);
      while (collision) {
        item.y = collision.y + collision.span;
        collision = hasCollision(item, placed);
      }
      placed.push(item);
    });
    unpositioned
      .sort((left, right) => domIndex(left.key) - domIndex(right.key))
      .forEach((item) => {
        for (let y = 0; ; y += 1) {
          let slotted = false;
          for (let x = 0; x < metrics.columns; x += 1) {
            if (!hasCollision({ x, y, span: item.span }, placed)) {
              item.x = x;
              item.y = y;
              slotted = true;
              break;
            }
          }
          if (slotted) {
            break;
          }
        }
        placed.push(item);
      });
    return Object.fromEntries(placed.map((item) => [item.key, { x: item.x, y: item.y }]));
  };

  const applyLayout = (layout) => {
    cardList().forEach((card) => {
      const position = layout[card.dataset.cardKey] || { x: 0, y: 0 };
      card.style.gridColumn = `${position.x + 1} / span 1`;
      card.style.gridRow = `${position.y + 1} / span ${cardSpan(card)}`;
    });
  };

  try {
    const saved = JSON.parse(localStorage.getItem(orderKey) || "[]");
    if (Array.isArray(saved) && saved.length) {
      const byKey = new Map(cardList().map((card) => [card.dataset.cardKey, card]));
      saved.forEach((key) => {
        const card = byKey.get(key);
        if (card) {
          grid.appendChild(card);
        }
      });
    }
  } catch (_error) {
    // ignore corrupt saved order
  }
  const sizes = savedSizes();
  autoCardList().forEach((card) => {
    if (sizes[card.dataset.cardKey] === "expanded") {
      card.dataset.cardExpanded = "true";
    }
    syncSizeToggle(card);
  });

  let activeLayout = resolveLayout(Object.keys(loadLayout()).length ? loadLayout() : layoutFromDom());
  applyLayout(activeLayout);

  // Re-pack after the card picker shows/hides a card.
  grid.relayoutSummaryCards = () => {
    activeLayout = resolveLayout(activeLayout);
    applyLayout(activeLayout);
    persistLayout(activeLayout);
  };

  grid.querySelectorAll("[data-card-size-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const card = button.closest("[data-card-key]");
      if (!card) {
        return;
      }
      if (card.dataset.cardExpanded === "true") {
        delete card.dataset.cardExpanded;
      } else {
        card.dataset.cardExpanded = "true";
      }
      syncSizeToggle(card);
      persistSizes();
      activeLayout = resolveLayout(activeLayout, card.dataset.cardKey);
      applyLayout(activeLayout);
      persistLayout(activeLayout);
    });
  });

  const persist = () => {
    try {
      localStorage.setItem(orderKey, JSON.stringify(cardList().map((card) => card.dataset.cardKey)));
    } catch (_error) {
      // persistence is optional
    }
  };

  // Pointer-based drag with explicit grid slots. The layout is sparse on
  // purpose: empty half-slots can remain between cards, just like a dashboard
  // grid with manual placement.
  let dragCard = null;
  let placeholder = null;
  let previewLayout = null;
  let startX = 0;
  let startY = 0;
  let offsetX = 0;
  let offsetY = 0;
  let active = false;

  const beginDrag = () => {
    active = true;
    const rect = dragCard.getBoundingClientRect();
    activeLayout = resolveLayout(activeLayout);
    applyLayout(activeLayout);
    offsetX = startX - rect.left;
    offsetY = startY - rect.top;
    placeholder = document.createElement("div");
    placeholder.className = "summary-card card-placeholder";
    placeholder.dataset.cardSize = dragCard.dataset.cardSize || "full";
    if (dragCard.dataset.cardExpanded === "true") {
      placeholder.dataset.cardExpanded = "true";
    }
    const startPosition = activeLayout[dragCard.dataset.cardKey] || cardGridPosition(dragCard, gridMetrics());
    placeholder.style.gridColumn = `${startPosition.x + 1} / span 1`;
    placeholder.style.gridRow = `${startPosition.y + 1} / span ${cardSpan(dragCard)}`;
    grid.appendChild(placeholder);
    dragCard.style.width = `${rect.width}px`;
    dragCard.style.height = `${rect.height}px`;
    dragCard.style.position = "fixed";
    dragCard.style.left = `${rect.left}px`;
    dragCard.style.top = `${rect.top}px`;
    dragCard.style.zIndex = "1000";
    dragCard.style.pointerEvents = "none";
    dragCard.classList.add("dragging");
    document.body.classList.add("cards-dragging");
  };

  const onMove = (event) => {
    if (!dragCard) {
      return;
    }
    if (!active) {
      if (Math.hypot(event.clientX - startX, event.clientY - startY) < 6) {
        return;
      }
      beginDrag();
    }
    event.preventDefault();
    dragCard.style.left = `${event.clientX - offsetX}px`;
    dragCard.style.top = `${event.clientY - offsetY}px`;
    const target = pointerGridPosition(event, gridMetrics());
    if (!target) {
      return;
    }
    previewLayout = resolveLayout(
      {
        ...activeLayout,
        [dragCard.dataset.cardKey]: target,
      },
      dragCard.dataset.cardKey
    );
    const previewPosition = previewLayout[dragCard.dataset.cardKey] || target;
    placeholder.style.gridColumn = `${previewPosition.x + 1} / span 1`;
    placeholder.style.gridRow = `${previewPosition.y + 1} / span ${cardSpan(dragCard)}`;
    applyLayout(previewLayout);
  };

  const onUp = () => {
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    if (active && dragCard) {
      activeLayout = previewLayout || activeLayout;
      dragCard.style.cssText = "";
      dragCard.classList.remove("dragging");
      placeholder?.remove();
      applyLayout(activeLayout);
      persistLayout(activeLayout);
      persist();
    }
    document.body.classList.remove("cards-dragging");
    dragCard = null;
    placeholder = null;
    previewLayout = null;
    active = false;
  };

  grid.addEventListener("mousedown", (event) => {
    if (event.button !== 0) {
      return;
    }
    const card = event.target.closest("[data-card-key]");
    if (!card || card.parentElement !== grid) {
      return;
    }
    // Let clicks on interactive controls behave normally.
    if (event.target.closest("a, button, input, textarea, select, summary, details, label")) {
      return;
    }
    dragCard = card;
    startX = event.clientX;
    startY = event.clientY;
    active = false;
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
};

const initNodeReload = (root = document) => {
  root.querySelectorAll("[data-node-reload]").forEach((select) => {
    if (select.dataset.initialized === "true") {
      return;
    }
    select.dataset.initialized = "true";
    select.addEventListener("change", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("node", select.value);
      loadSoftNavigation(url);
    });
  });
};

const initCopyButtons = (root = document) => {
  root.querySelectorAll("[data-copy-command]").forEach((button) => {
    if (button.dataset.copyInit === "true") {
      return;
    }
    button.dataset.copyInit = "true";
    button.addEventListener("click", async () => {
      const code = button.closest(".health-command")?.querySelector("[data-health-command]");
      const text = (code?.textContent || "").trim();
      if (!text) {
        return;
      }
      const original = button.textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
        window.setTimeout(() => {
          button.textContent = original;
        }, 1500);
      } catch (_error) {
        // Non-secure context or no clipboard API — select the text so the user can copy manually.
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
    });
  });
};

const initSummaryCardPicker = (root = document) => {
  root.querySelectorAll("[data-summary-card-picker]").forEach((picker) => {
    if (picker.dataset.initialized === "true") {
      return;
    }
    const grid = document.querySelector("[data-summary-cards]");
    const panel = picker.querySelector("[data-summary-card-picker-panel]");
    if (!grid || !panel) {
      return;
    }
    picker.dataset.initialized = "true";
    const storageKey = "pve-helper-vm-summary-hidden";
    let hidden = [];
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || "[]");
      if (Array.isArray(stored)) {
        hidden = stored.filter((key) => typeof key === "string");
      }
    } catch (_error) {
      hidden = [];
    }
    const cardLabel = (card) => card.querySelector(".panel-heading h2")?.textContent?.trim() || card.dataset.cardKey;
    // The picker is a pure show/hide list (cards are reordered by dragging the
    // cards themselves), so list it alphabetically for easy scanning.
    const cards = Array.from(grid.querySelectorAll(":scope > [data-card-key]")).sort((a, b) =>
      cardLabel(a).localeCompare(cardLabel(b))
    );
    const applyHidden = () => {
      cards.forEach((card) => {
        card.style.display = hidden.includes(card.dataset.cardKey) ? "none" : "";
      });
      // display:none uses inline style, which the picker filters on; re-pack.
      grid.relayoutSummaryCards?.();
    };

    panel.innerHTML = "";
    cards.forEach((card) => {
      const key = card.dataset.cardKey;
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = !hidden.includes(key);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          hidden = hidden.filter((item) => item !== key);
        } else if (!hidden.includes(key)) {
          hidden.push(key);
        }
        try {
          localStorage.setItem(storageKey, JSON.stringify(hidden));
        } catch (_error) {
          // persistence is optional
        }
        applyHidden();
      });
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(` ${cardLabel(card)}`));
      panel.appendChild(label);
    });
    applyHidden();

    document.addEventListener("click", (event) => {
      if (picker.open && !picker.contains(event.target)) {
        picker.open = false;
      }
    });
  });
};

export {
  initColumnPickers,
  initCopyButtons,
  initGuestListFilter,
  initNodeReload,
  initResizableColumns,
  initSortableTables,
  initSpaceCharts,
  initSummaryCardPicker,
  initSummaryCards,
  initTableFilters,
};
