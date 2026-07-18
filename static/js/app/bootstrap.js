import { initClusterFilter } from "./cluster-filter.js";
import { initConsolePages } from "./console.js";
import { initContextMenu } from "./context-menus.js";
import { initBackupRestoreForms, initGuestActionForms } from "./guest-actions.js";
import { initHardwareEditor } from "./hardware.js";
import { initSoftNavigation, setPageInitializer } from "./navigation.js";
import { initRecentTasks } from "./recent-tasks.js";
import { initVmRegister } from "./register.js";
import {
  initAuditExportDialog,
  initAutoSubmitForms,
  initConfirmForms,
  initScanActions,
  initScheduledRuns,
  initScheduledTaskForms,
} from "./scheduling.js";
import {
  applyGuestNameStyle,
  applyIpVersionStyle,
  applySidebarState,
  applyTaskbarState,
  applyTheme,
  createIcons,
  initGlobalSearch,
  initGuestNameToggle,
  initIpVersionToggle,
  initSidebarControls,
  initTaskbarToggle,
  initThemeToggle,
  initTreeModules,
  preferredGuestNameStyle,
  preferredIpVersionStyle,
  preferredTheme,
  sidebarCollapsedKey,
  sortGuestList,
  taskbarKey,
} from "./shell.js";
import { initConfirmedFileActions, initStorageFileManagers } from "./storage-browser.js";
import {
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
} from "./tables.js";
import { initTags } from "./tags.js";
import {
  initGuestAgentSummaries,
  initVmOverviewAgentInfo,
  initVmOverviewSelection,
  initVmOverviewSnapshotInfo,
  initVmStatusRefresh,
} from "./vm-overview.js";

const initPage = (root = document) => {
  initTags(root);
  initHardwareEditor(root);
  initVmRegister(root);
  initGuestActionForms(root);
  initCopyButtons(root);
  initBackupRestoreForms(root);
  initGuestListFilter(root);
  initClusterFilter(root);
  sortGuestList(document.documentElement.dataset.guestNameStyle !== "name-only");
  initNodeReload(root);
  initSummaryCards(root);
  initSummaryCardPicker(root);
  initAutoSubmitForms(root);
  initAuditExportDialog(root);
  initScanActions(root);
  initStorageFileManagers(root);
  initConfirmedFileActions(root);
  initConfirmForms(root);
  initScheduledTaskForms(root);
  initScheduledRuns(root);
  initSpaceCharts(root);
  initTableFilters(root);
  initColumnPickers(root);
  initResizableColumns(root);
  initSortableTables(root);
  initVmOverviewSelection(root);
  initVmOverviewAgentInfo(root);
  initVmOverviewSnapshotInfo(root);
  initVmStatusRefresh(root);
  initGuestAgentSummaries(root);
  initConsolePages(root);
  applyIpVersionStyle(document.documentElement.dataset.ipVersionStyle || "all");
  createIcons();
};

setPageInitializer(initPage);

export const initShell = () => {
  applyTheme(preferredTheme());
  applyGuestNameStyle(preferredGuestNameStyle());
  applyIpVersionStyle(preferredIpVersionStyle());
  try {
    applyTaskbarState(localStorage.getItem(taskbarKey) === "true");
  } catch (_error) {
    applyTaskbarState(false);
  }
  try {
    applySidebarState(localStorage.getItem(sidebarCollapsedKey) === "true");
  } catch (_error) {
    applySidebarState(false);
  }
  initThemeToggle();
  initGuestNameToggle();
  initIpVersionToggle();
  initTaskbarToggle();
  initSidebarControls();
  initGlobalSearch();
  initTreeModules(document);
  initContextMenu();
  initSoftNavigation();
  initPage(document);
  initRecentTasks();
};
