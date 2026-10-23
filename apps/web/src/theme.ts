const themeKey = "openengine.theme";

export function loadTheme(): boolean {
  let dark = false;
  try {
    dark = window.localStorage.getItem(themeKey) === "dark";
  } catch {
    // Keep the default when browser storage is unavailable.
  }
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  return dark;
}

export function setDarkMode(dark: boolean): void {
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try {
    window.localStorage.setItem(themeKey, dark ? "dark" : "light");
  } catch {
    // The toggle still works for this session without browser storage.
  }
}
