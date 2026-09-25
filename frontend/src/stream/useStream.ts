/**
 * Подписка на SSE /api/v1/stream. Живое; FE-08 добавляет тосты, BE-08 — хранение.
 *
 * Одно соединение EventSource на вкладку: его открывает AuthContext после входа
 * (startStream) и закрывает при выходе (stopStream). Экраны читают общее
 * состояние через useStream() и подписываются на события через useStreamEvents().
 *
 * Формат кадра задаёт backend/app/routers/stream.py: `event: <kind>` и
 * `data: {kind, severity, title, ts, payload}` (Broker.publish в
 * services/notifications.py); первый кадр — `hello` с пустым объектом. В
 * OpenAPI поток не описан, поэтому тип кадра проверяется здесь при разборе.
 *
 * Переподключение. Обрыв сети браузер лечит сам (сервер шлёт retry: 5000). Если
 * соединение закрыто окончательно (например, сервер ответил 401), ждём с
 * нарастающей паузой до 30 с, проверяем сессию через /me и открываем заново;
 * на 401 клиент API сам уводит на /login, и AuthContext закрывает поток.
 */
import { useEffect, useRef, useSyncExternalStore } from "react";

import { api, type Schemas } from "../api/client";

export type StreamKind = Schemas["NotificationItem"]["kind"];
export type Severity = Schemas["NotificationItem"]["severity"];

/** Подписи видов событий. Record ловит на typecheck новый вид в контракте. */
export const KIND_TITLES: Record<StreamKind, string> = {
  "alert.new": "Новый прогноз",
  "event.alarm": "Тревожное событие",
  "run.finished": "Расчёт завершён",
  "workorder.changed": "Заявка изменена",
};

const KINDS = Object.keys(KIND_TITLES) as StreamKind[];
const SEVERITIES: readonly Severity[] = ["info", "warning", "critical"];
const KEEP = 50;
const MAX_DELAY_MS = 30_000;

export interface StreamEvent {
  /** Порядковый номер в сессии вкладки: ключ списка. */
  seq: number;
  kind: StreamKind;
  severity: Severity;
  title: string;
  ts: string;
  payload: Record<string, unknown>;
}

export type Connection = "idle" | "connecting" | "open" | "reconnecting";

export interface StreamSnapshot {
  connection: Connection;
  /** Последние 50 событий, новые сверху. */
  events: StreamEvent[];
  unread: number;
}

const IDLE: StreamSnapshot = { connection: "idle", events: [], unread: 0 };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isSeverity(value: unknown): value is Severity {
  return SEVERITIES.some((s) => s === value);
}

class StreamStore {
  private snapshot: StreamSnapshot = IDLE;
  private readonly subscribers = new Set<() => void>();
  private readonly listeners = new Set<(event: StreamEvent) => void>();
  private source: EventSource | null = null;
  private timer: number | null = null;
  private attempts = 0;
  private seq = 0;
  private wanted = false;

  subscribe = (callback: () => void): (() => void) => {
    this.subscribers.add(callback);
    return () => this.subscribers.delete(callback);
  };

  getSnapshot = (): StreamSnapshot => this.snapshot;

  listen(listener: (event: StreamEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  start(): void {
    this.wanted = true;
    if (this.source || this.timer !== null) return;
    this.open();
  }

  /** Закрывает поток и забывает события: следующий вход может быть под другой ролью. */
  stop(): void {
    this.wanted = false;
    this.closeSource();
    if (this.timer !== null) window.clearTimeout(this.timer);
    this.timer = null;
    this.attempts = 0;
    this.set(IDLE);
  }

  markAllRead = (): void => {
    if (this.snapshot.unread > 0) this.set({ ...this.snapshot, unread: 0 });
  };

  private set(next: StreamSnapshot): void {
    this.snapshot = next;
    this.subscribers.forEach((callback) => callback());
  }

  private open(): void {
    this.set({ ...this.snapshot, connection: this.attempts > 0 ? "reconnecting" : "connecting" });
    const source = new EventSource("/api/v1/stream", { withCredentials: true });
    this.source = source;
    source.addEventListener("hello", () => {
      this.attempts = 0;
      this.set({ ...this.snapshot, connection: "open" });
    });
    for (const kind of KINDS) {
      source.addEventListener(kind, (message: MessageEvent<string>) => this.receive(kind, message.data));
    }
    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        this.closeSource();
        this.scheduleReconnect();
      } else {
        this.set({ ...this.snapshot, connection: "reconnecting" });
      }
    };
  }

  private closeSource(): void {
    this.source?.close();
    this.source = null;
  }

  private scheduleReconnect(): void {
    if (!this.wanted) return;
    const delay = Math.min(MAX_DELAY_MS, 1000 * 2 ** this.attempts);
    this.attempts += 1;
    this.set({ ...this.snapshot, connection: "reconnecting" });
    this.timer = window.setTimeout(() => {
      this.timer = null;
      void this.reconnect();
    }, delay);
  }

  private async reconnect(): Promise<void> {
    try {
      const { response } = await api.GET("/api/v1/me");
      // 401: клиент API уже ведёт на /login, AuthContext вызовет stop().
      if (response.status === 401) return;
    } catch {
      // Сервер недоступен: пробуем открыть поток, ошибка вернёт нас сюда с паузой больше.
    }
    if (this.wanted && !this.source) this.open();
  }

  private receive(kind: StreamKind, data: string): void {
    let body: unknown;
    try {
      body = JSON.parse(data);
    } catch {
      return;
    }
    if (!isRecord(body)) return;
    const event: StreamEvent = {
      seq: ++this.seq,
      kind,
      severity: isSeverity(body.severity) ? body.severity : "info",
      title: typeof body.title === "string" && body.title ? body.title : KIND_TITLES[kind],
      ts: typeof body.ts === "string" ? body.ts : new Date().toISOString(),
      payload: isRecord(body.payload) ? body.payload : {},
    };
    this.set({
      connection: this.snapshot.connection,
      events: [event, ...this.snapshot.events].slice(0, KEEP),
      unread: this.snapshot.unread + 1,
    });
    this.listeners.forEach((listener) => listener(event));
  }
}

const store = new StreamStore();

export function startStream(): void {
  store.start();
}

export function stopStream(): void {
  store.stop();
}

/** Общее состояние потока: соединение, последние события, счётчик непрочитанных. */
export function useStream(): StreamSnapshot & { markAllRead: () => void } {
  const snapshot = useSyncExternalStore(store.subscribe, store.getSnapshot);
  return { ...snapshot, markAllRead: store.markAllRead };
}

/**
 * Перечитывает данные экрана по событиям SSE. Один прогон run-daily шлёт
 * alert.new на каждый новый прогноз, поэтому пачка событий за delayMs
 * сливается в один перезапрос.
 */
export function useReloadOn(
  kinds: readonly StreamKind[],
  reload: () => void,
  { enabled = true, delayMs = 1000 }: { enabled?: boolean; delayMs?: number } = {},
): void {
  const timer = useRef<number | null>(null);
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  useStreamEvents(kinds, () => {
    if (!enabled || timer.current !== null) return;
    timer.current = window.setTimeout(() => {
      timer.current = null;
      reloadRef.current();
    }, delayMs);
  });
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = null;
    },
    [],
  );
}

/** Вызывает handler на каждое событие перечисленных видов, пока экран открыт. */
export function useStreamEvents(kinds: readonly StreamKind[], handler: (event: StreamEvent) => void): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const key = kinds.join(",");
  useEffect(() => {
    const wanted = new Set(key.split(","));
    return store.listen((event) => {
      if (wanted.has(event.kind)) handlerRef.current(event);
    });
  }, [key]);
}
