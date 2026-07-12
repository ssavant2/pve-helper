import {
  applyGuestNameStyle,
  applyIpVersionStyle,
  applySidebarState,
  applyTaskbarState,
  applyTheme,
  createIcons,
  initAuditExportDialog,
  initAutoSubmitForms,
  initBackupRestoreForms,
  initColumnPickers,
  initConfirmedFileActions,
  initConfirmForms,
  initConsolePages,
  initContextMenu,
  initCopyButtons,
  initGlobalSearch,
  initGuestActionForms,
  initGuestAgentSummaries,
  initGuestListFilter,
  initGuestNameToggle,
  initHardwareEditor,
  initIpVersionToggle,
  initNodeReload,
  initRecentTasks,
  initResizableColumns,
  initScanActions,
  initScheduledRuns,
  initScheduledTaskForms,
  initSidebarControls,
  initSoftNavigation,
  initSortableTables,
  initSpaceCharts,
  initStorageFileManagers,
  initSummaryCardPicker,
  initSummaryCards,
  initTableFilters,
  initTaskbarToggle,
  initThemeToggle,
  initTreeModules,
  initVmOverviewAgentInfo,
  initVmOverviewSelection,
  initVmOverviewSnapshotInfo,
  initVmRegister,
  initVmStatusRefresh,
  preferredGuestNameStyle,
  preferredIpVersionStyle,
  preferredTheme,
  sidebarCollapsedKey,
  sortGuestList,
  taskbarKey,
} from "./main.js";

const initPage = (root = document) => {
  initHardwareEditor(root);
  initVmRegister(root);
  initGuestActionForms(root);
  initCopyButtons(root);
  initBackupRestoreForms(root);
  initGuestListFilter(root);
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
