(() => {
  try {
    let theme = localStorage.getItem("pve-helper-theme");
    if (theme !== "light" && theme !== "dark") {
      theme = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  } catch (_error) {
    document.documentElement.dataset.theme = "light";
  }

  try {
    const guestNameStyle = localStorage.getItem("pve-helper-guest-name-style");
    document.documentElement.dataset.guestNameStyle = guestNameStyle === "name-only" ? "name-only" : "id-name";
  } catch (_error) {
    document.documentElement.dataset.guestNameStyle = "id-name";
  }

  try {
    const ipVersionStyle = localStorage.getItem("pve-helper-ip-version-style");
    document.documentElement.dataset.ipVersionStyle = ipVersionStyle === "ipv4-only" ? "ipv4-only" : "all";
  } catch (_error) {
    document.documentElement.dataset.ipVersionStyle = "all";
  }
})();
