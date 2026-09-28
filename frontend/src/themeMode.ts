/**
 * Тема оформления: как в системе, светлая или тёмная.
 *
 * Выбор хранится в localStorage только как удобство на этом устройстве: если
 * хранилище недоступно (приватное окно, запрет сайта), тема просто следует
 * системе. Токены обеих тем — в tokens.css, здесь только атрибут data-theme.
 */
import { useEffect, useState } from "react";

type ThemeMode = "system" | "light" | "dark";

const KEY = "mkl.theme";
const ORDER: ThemeMode[] = ["system", "light", "dark"];

export const THEME_TITLE: Record<ThemeMode, string> = {
  system: "Тема как в системе",
  light: "Светлая тема",
  dark: "Тёмная тема",
};

function read(): ThemeMode {
  try {
    const value = window.localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

function apply(mode: ThemeMode): void {
  const root = document.documentElement;
  if (mode === "system") delete root.dataset.theme;
  else root.dataset.theme = mode;
}

/** Вызывается до первого рендера, чтобы страница не мигала светлой темой. */
export function applyStoredTheme(): void {
  apply(read());
}

export function useThemeMode(): { mode: ThemeMode; next: () => void } {
  const [mode, setMode] = useState<ThemeMode>(read);
  useEffect(() => {
    apply(mode);
    try {
      if (mode === "system") window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, mode);
    } catch {
      // хранилище недоступно: выбор действует до перезагрузки
    }
  }, [mode]);
  const next = () => setMode((current) => ORDER[(ORDER.indexOf(current) + 1) % ORDER.length]);
  return { mode, next };
}
