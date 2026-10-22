/** Light or dark: the person's choice if they have made one, otherwise the
 *  system's. The choice is kept in the browser and laid on the root element as
 *  `data-theme`, which is all the stylesheet reads. */

import { useState } from "react";

export type Theme = "light" | "dark";

const THEME_KEY = "engine.theme";

export function storedTheme(): Theme {
  const saved = window.localStorage.getItem(THEME_KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme;
}

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(storedTheme);
  const toggle = () => {
    const next = theme === "dark" ? "light" : "dark";
    window.localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
    setTheme(next);
  };
  return [theme, toggle];
}
