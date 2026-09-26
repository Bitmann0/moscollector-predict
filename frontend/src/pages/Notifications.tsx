import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime } from "../format";
import { KIND_TITLES, useReloadOn, useStream, type Connection, type Severity, type StreamEvent } from "../stream/useStream";

type Notification = Schemas["NotificationItem"];
const CONNECTION_TEXT: Record<Connection, string> = { idle: "не подключён", connecting: "подключается", open: "на связи", reconnecting: "переподключается" };
const SEVERITY_TEXT: Record<Severity, string> = { info: "Информация", warning: "Важно", critical: "Срочно" };

function linkOf(kind: Notification["kind"], payload: Record<string, unknown> | undefined): string | null {
  const id = payload?.id;
  if (kind === "alert.new" && typeof id === "string") return `/forecasts/${encodeURIComponent(id)}`;
  if (kind === "workorder.changed") return typeof id === "string" ? `/work-orders?open=${encodeURIComponent(id)}` : "/work-orders";
  if (kind === "event.alarm") return "/events";
  return null;
}

export function Notifications() {
  const { events, unread, connection, markAllRead } = useStream();
  const [page, setPage] = useState(1);
  const persisted = useLoad(() => api.GET("/api/v1/notifications", { params: { query: { page, page_size: 100 } } }), [page]);
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  useReloadOn(["alert.new", "event.alarm", "run.finished", "workorder.changed"], persisted.reload);
  useEffect(() => { if (unread > 0) markAllRead(); }, [unread, markAllRead]);

  async function markRead(item: Notification) {
    if (item.read) return;
    setBusy(true); setActionError(null);
    try {
      const result = await api.POST("/api/v1/notifications/{notification_id}/read", { params: { path: { notification_id: item.id } } });
      if (!result.response.ok) throw new Error(errorText(result.error, result.response));
      persisted.reload();
    } catch (error) { setActionError(error instanceof Error ? error.message : errorText(null, undefined)); }
    finally { setBusy(false); }
  }
  async function markAll() {
    setBusy(true); setActionError(null);
    try {
      const first = await api.GET("/api/v1/notifications", { params: { query: { page: 1, page_size: 500 } } });
      if (!first.data || !first.response.ok) throw new Error(errorText(first.error, first.response));
      const unreadItems = first.data.items.filter((item) => !item.read);
      for (let nextPage = 2; nextPage <= Math.ceil(first.data.total / 500); nextPage++) {
        const result = await api.GET("/api/v1/notifications", { params: { query: { page: nextPage, page_size: 500 } } });
        if (!result.data || !result.response.ok) throw new Error(errorText(result.error, result.response));
        unreadItems.push(...result.data.items.filter((item) => !item.read));
      }
      for (let index = 0; index < unreadItems.length; index += 25) {
        const results = await Promise.all(unreadItems.slice(index, index + 25).map((item) => api.POST("/api/v1/notifications/{notification_id}/read", { params: { path: { notification_id: item.id } } })));
        const failed = results.find((result) => !result.response.ok);
        if (failed) throw new Error(errorText(failed.error, failed.response));
      }
      persisted.reload();
    } catch (error) { setActionError(error instanceof Error ? error.message : errorText(null, undefined)); persisted.reload(); }
    finally { setBusy(false); }
  }

  return <section>
    <PageHeader eyebrow="Оперативная лента" title="Центр уведомлений" description="Прогнозы, тревоги, расчёты и изменения заявок" actions={<div className={`stream-pill stream-pill--${connection}`}><i />Поток {CONNECTION_TEXT[connection]}</div>} />
    <div className="notifications-toolbar"><div className="segmented">{(["all", "critical", "warning", "info"] as const).map((item) => <button key={item} type="button" className={severity === item ? "active" : ""} onClick={() => setSeverity(item)}>{item === "all" ? "Все" : SEVERITY_TEXT[item]}</button>)}</div><button className="button" type="button" disabled={busy || (persisted.data?.total ?? 0) === 0} onClick={() => void markAll()}>{busy ? "Обновление…" : "Отметить всё прочитанным"}</button></div>
    {actionError && <p className="form-error" role="alert">{actionError}</p>}
    <Loaded load={persisted}>{(data) => { const stored = data.items.filter((item) => severity === "all" || item.severity === severity); const live = events.filter((item) => severity === "all" || item.severity === severity); return <div className="notifications-layout"><div>
      {stored.length > 0 && <><h2 className="list-heading">История <span>{data.total}</span></h2><div className="notification-list">{stored.map((item) => <NotificationRow key={item.id} item={item} onRead={() => void markRead(item)} />)}</div></>}
      {data.total > data.page_size && <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={setPage} />}
      {severity !== "all" && <p className="chart-note">Фильтр важности применяется к текущей странице истории и новым событиям.</p>}
      <h2 className="list-heading">Текущая сессия <span>{live.length}</span></h2>{live.length ? <div className="notification-list">{live.map((item) => <LiveRow key={item.seq} item={item} />)}</div> : <StateView state="empty" detail="Новые события появятся здесь без перезагрузки страницы." />}
    </div><aside className="panel notification-help"><Icon name="bell" /><h3>Как это работает</h3><p>Критические сигналы показываются поверх любого экрана. История с сервера сохраняется после перезагрузки.</p><dl><div><dt>В истории</dt><dd>{data.total}</dd></div><div><dt>Непрочитано на странице</dt><dd>{data.items.filter((item) => !item.read).length}</dd></div><div><dt>В этой сессии</dt><dd>{events.length}</dd></div></dl></aside></div>; }}</Loaded>
  </section>;
}

function NotificationRow({ item, onRead }: { item: Notification; onRead: () => void }) {
  const link = linkOf(item.kind, item.payload);
  return <article className={`notification-row notification-row--${item.severity} ${item.read ? "notification-row--read" : ""}`}><div className="notification-row__icon"><Icon name={item.kind === "workorder.changed" ? "wrench" : item.kind === "alert.new" ? "forecast" : "bell"} /></div><div><div className="notification-row__meta"><span>{KIND_TITLES[item.kind]}</span><time>{fmtDateTime(item.ts)}</time></div><strong>{link ? <Link to={link} onClick={onRead}>{item.title}</Link> : item.title}</strong><small>{SEVERITY_TEXT[item.severity]}{!item.read && " · новое"}</small></div>{!item.read && <button type="button" onClick={onRead} title="Отметить прочитанным">✓</button>}</article>;
}

function LiveRow({ item }: { item: StreamEvent }) {
  const link = linkOf(item.kind, item.payload);
  return <article className={`notification-row notification-row--${item.severity}`}><div className="notification-row__icon"><Icon name={item.kind === "workorder.changed" ? "wrench" : item.kind === "alert.new" ? "forecast" : "bell"} /></div><div><div className="notification-row__meta"><span>{KIND_TITLES[item.kind]}</span><time>{fmtDateTime(item.ts)}</time></div><strong>{link ? <Link to={link}>{item.title}</Link> : item.title}</strong><small>{SEVERITY_TEXT[item.severity]} · получено сейчас</small></div></article>;
}
