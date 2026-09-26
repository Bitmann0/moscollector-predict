import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime } from "../format";
import { KIND_TITLES, useReloadOn, useStream, type Connection, type Severity, type StreamEvent } from "../stream/useStream";

type Notification = Schemas["NotificationItem"];
const CONNECTION_TEXT: Record<Connection, string> = { idle: "не подключён", connecting: "подключается", open: "на связи", reconnecting: "переподключается" };
const SEVERITY_TEXT: Record<Severity, string> = { info: "Информация", warning: "Важно", critical: "Срочно" };

function linkOf(kind: Notification["kind"], payload: Record<string, unknown> | undefined): string | null {
  const id = payload?.id;
  if (kind === "alert.new" && typeof id === "string") return `/forecasts/${encodeURIComponent(id)}`;
  if (kind === "workorder.changed") return "/work-orders";
  if (kind === "event.alarm") return "/events";
  return null;
}

export function Notifications() {
  const { events, unread, connection, markAllRead } = useStream();
  const persisted = useLoad(() => api.GET("/api/v1/notifications", { params: { query: { page: 1, page_size: 100 } } }), []);
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [busy, setBusy] = useState(false);
  useReloadOn(["alert.new", "event.alarm", "run.finished", "workorder.changed"], persisted.reload);
  useEffect(() => { if (unread > 0) markAllRead(); }, [unread, markAllRead]);

  async function markRead(item: Notification) {
    if (item.read) return;
    setBusy(true);
    try { await api.POST("/api/v1/notifications/{notification_id}/read", { params: { path: { notification_id: item.id } } }); persisted.reload(); } finally { setBusy(false); }
  }
  async function markAll() {
    const items = persisted.data?.items.filter((item) => !item.read) ?? [];
    if (!items.length) return;
    setBusy(true);
    try { await Promise.all(items.map((item) => api.POST("/api/v1/notifications/{notification_id}/read", { params: { path: { notification_id: item.id } } }))); persisted.reload(); } finally { setBusy(false); }
  }

  return <section>
    <PageHeader eyebrow="Оперативная лента" title="Центр уведомлений" description="Прогнозы, тревоги, расчёты и изменения заявок" actions={<div className={`stream-pill stream-pill--${connection}`}><i />Поток {CONNECTION_TEXT[connection]}</div>} />
    <div className="notifications-toolbar"><div className="segmented">{(["all", "critical", "warning", "info"] as const).map((item) => <button key={item} type="button" className={severity === item ? "active" : ""} onClick={() => setSeverity(item)}>{item === "all" ? "Все" : SEVERITY_TEXT[item]}</button>)}</div><button className="button" type="button" disabled={busy || !persisted.data?.items.some((item) => !item.read)} onClick={() => void markAll()}>Отметить всё прочитанным</button></div>
    <Loaded load={persisted}>{(data) => { const stored = data.items.filter((item) => severity === "all" || item.severity === severity); const live = events.filter((item) => severity === "all" || item.severity === severity); return <div className="notifications-layout"><div>
      {stored.length > 0 && <><h2 className="list-heading">История <span>{data.total}</span></h2><div className="notification-list">{stored.map((item) => <NotificationRow key={item.id} item={item} onRead={() => void markRead(item)} />)}</div></>}
      <h2 className="list-heading">Текущая сессия <span>{live.length}</span></h2>{live.length ? <div className="notification-list">{live.map((item) => <LiveRow key={item.seq} item={item} />)}</div> : <StateView state="empty" detail="Новые события появятся здесь без перезагрузки страницы." />}
    </div><aside className="panel notification-help"><Icon name="bell" /><h3>Как это работает</h3><p>Критические сигналы показываются поверх любого экрана. История с сервера сохраняется после перезагрузки.</p><dl><div><dt>В истории</dt><dd>{data.total}</dd></div><div><dt>Непрочитано</dt><dd>{data.items.filter((item) => !item.read).length}</dd></div><div><dt>В этой сессии</dt><dd>{events.length}</dd></div></dl></aside></div>; }}</Loaded>
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
