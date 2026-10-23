/** The colour theme the user chose in settings, remembered in this browser. */

export type Theme = "light" | "dark";

const THEME_KEY = "engine.theme";

export function savedTheme(): Theme {
  try {
    return window.localStorage.getItem(THEME_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export function saveTheme(theme: Theme) {
  applyTheme(theme);
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Storage can be unavailable; the choice still holds for this page.
  }
}
