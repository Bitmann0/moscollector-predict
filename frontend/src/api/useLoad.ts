/**
 * Загрузка данных для экрана. Живое.
 *
 * Принимает функцию, которая вызывает api.GET(...) и возвращает ответ openapi-fetch
 * как есть; тип данных выводится из schema.d.ts. При reload() прежние данные
 * остаются на экране, пока идёт запрос: таблица с автообновлением не мигает.
 * При смене deps (другой фильтр, другая карточка) прежние данные сбрасываются.
 * Ответ устаревшего запроса отбрасывается.
 */
import { useCallback, useEffect, useRef, useState, type DependencyList } from "react";

import { errorText } from "./client";

export type LoadState<T> =
  | { status: "loading"; data?: T }
  | { status: "error"; message: string; data?: T }
  | { status: "ok"; data: T };

export type Load<T> = LoadState<T> & { reload: () => void };

interface ApiResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

export function useLoad<T>(fetcher: () => Promise<ApiResult<T>>, deps: DependencyList): Load<T> {
  const [state, setState] = useState<LoadState<T>>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  const lastTick = useRef(tick);
  // Последняя версия fetcher: зависимости запроса задаёт deps, а не сама функция.
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    const id = ++seq.current;
    const isReload = tick !== lastTick.current;
    lastTick.current = tick;
    setState((prev) => ({ status: "loading", data: isReload ? prev.data : undefined }));
    fetcherRef
      .current()
      .then(({ data, error, response }) => {
        if (id !== seq.current) return;
        if (data !== undefined && response.ok) setState({ status: "ok", data });
        else setState((prev) => ({ status: "error", message: errorText(error, response), data: prev.data }));
      })
      .catch(() => {
        if (id !== seq.current) return;
        setState((prev) => ({ status: "error", message: errorText(null, undefined), data: prev.data }));
      });
    // deps передаёт вызывающий экран; tick — ручной перезапрос.
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}
