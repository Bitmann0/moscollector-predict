import { useEffect, useState } from "react";

/** Небольшие пользовательские настройки интерфейса между перезагрузками вкладки. */
export function usePersistentBoolean(key: string, initial: boolean): [boolean, (value: boolean) => void] {
  const [value, setValue] = useState(() => {
    try { const stored = window.localStorage.getItem(key); return stored === null ? initial : stored === "true"; }
    catch { return initial; }
  });
  useEffect(() => { try { window.localStorage.setItem(key, String(value)); } catch { /* приватный режим может запрещать storage */ } }, [key, value]);
  return [value, setValue];
}
