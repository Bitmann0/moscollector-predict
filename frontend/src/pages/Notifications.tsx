import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/common";
import { StateView } from "../components/StateView";
import { fmtDateTime } from "../format";
import { KIND_TITLES, useReloadOn, useStream, type Connection, type Severity } from "../stream/useStream";
import { incidentGroupOf, title } from "../vocab";

type Notification = Schemas["NotificationItem"];
const CONNECTION_TEXT: Record<Connection, string> = { idle: "не подключён", connecting: "подключается", open: "на связи", reconnecting: "переподключается" };
const SEVERITY_TEXT: Record<Severity, string> = { info: "Информация", warning: "Важно", critical: "Срочно" };
const SEVERITY_FILTER: Record<Severity, string> = { info: "информационных", warning: "важных", critical: "срочных" };
const PAGE_SIZE = 50;
/** API не фильтрует по важности и прочтению. Фильтр смотрит последние записи одним запросом (500 — предел API), чтобы не ограничиваться текущей страницей. */
const FILTER_WINDOW = 500;

function linkOf(kind: Notification["kind"], payload: Record<string, unknown> | undefined): string | null {
  const id = payload?.id;
  if (kind === "alert.new" && typeof id === "string") return `/forecasts/${encodeURIComponent(id)}`;
  if (kind === "workorder.changed") return typeof id === "string" ? `/work-orders?open=${encodeURIComponent(id)}` : "/work-orders";
  // Тревожное сообщение с группой аварии открывает журнал уже с фильтром по этой группе.
  if (kind === "event.alarm") { const group = incidentGroupOf(payload?.incident_group); return group ? `/events?incident_group=${group}` : "/events"; }
  return null;
}

export function Notifications() {
  const { events, unread, connection, markAllRead } = useStream();
  const [page, setPage] = useState(1);
  const [severity, setSeverity] = useState<Severity | "all">("all");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const filtered = severity !== "all" || unreadOnly;
  const persisted = useLoad(() => api.GET("/api/v1/notifications", { params: { query: filtered ? { page: 1, page_size: FILTER_WINDOW } : { page, page_size: PAGE_SIZE } } }), [filtered, filtered ? 1 : page]);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<ReadonlySet<number>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  useReloadOn(["alert.new", "event.alarm", "run.finished", "workorder.changed"], persisted.reload);
  useEffect(() => { if (unread > 0) markAllRead(); }, [unread, markAllRead]);

  // Каждое событие потока backend сохраняет в историю и присылает её id: пришедшее в этой сессии помечаем в истории, а не дублируем отдельным списком.
  const sessionIds = new Set(events.map((event) => event.payload.notification_id).filter((id): id is number => typeof id === "number"));

  function changeFilter(next: { severity?: Severity | "all"; unreadOnly?: boolean }) {
    if (next.severity !== undefined) setSeverity(next.severity);
    if (next.unreadOnly !== undefined) setUnreadOnly(next.unreadOnly);
    setPage(1);
  }
  async function markRead(item: Notification) {
    if (item.read || pending.has(item.id)) return;
    setPending((ids) => new Set(ids).add(item.id)); setActionError(null);
    try {
      const result = await api.POST("/api/v1/notifications/{notification_id}/read", { params: { path: { notification_id: item.id } } });
      if (!result.response.ok) throw new Error(errorText(result.error, result.response));
      persisted.reload();
    } catch (error) { setActionError(error instanceof Error ? error.message : errorText(null, undefined)); }
    finally { setPending((ids) => { const next = new Set(ids); next.delete(item.id); return next; }); }
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

  const data = persisted.data;
  // Всё загружено и прочитано — отмечать нечего.
  const nothingUnread = data !== undefined && data.total <= data.items.length && data.items.every((item) => item.read);
  const rows = data ? data.items.filter((item) => (severity === "all" || item.severity === severity) && (!unreadOnly || !item.read)) : [];
  const total = !data ? 0 : filtered ? rows.length : data.total;
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const current = !data ? 1 : filtered ? Math.min(page, lastPage) : data.page;
  const visible = filtered ? rows.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE) : rows;
  const unreadLoaded = data ? data.items.filter((item) => !item.read).length : 0;
  const emptyText = severity !== "all" ? `Нет ${unreadOnly ? "непрочитанных " : ""}${SEVERITY_FILTER[severity]} уведомлений.` : unreadOnly ? "Все уведомления прочитаны." : "Новые уведомления появятся здесь без перезагрузки страницы.";

  return <section>
    <PageHeader eyebrow="Оперативная лента" title="Центр уведомлений" description="Прогнозы, тревоги, расчёты и изменения заявок" actions={<div className={`stream-pill stream-pill--${connection}`} role="status" title="Новые уведомления приходят по потоку событий без перезагрузки страницы"><i />Поток {CONNECTION_TEXT[connection]}</div>} />
    <div className="notifications-toolbar">
      <div className="segmented" role="group" aria-label="Важность">{(["all", "critical", "warning", "info"] as const).map((item) => <button key={item} type="button" className={severity === item ? "active" : ""} aria-pressed={severity === item} onClick={() => changeFilter({ severity: item })}>{item === "all" ? "Все" : SEVERITY_TEXT[item]}</button>)}</div>
      <div className="notifications-actions">
        <button type="button" className={`notifications-unread${unreadOnly ? " active" : ""}`} aria-pressed={unreadOnly} onClick={() => changeFilter({ unreadOnly: !unreadOnly })}><i aria-hidden="true" />Непрочитанные</button>
        <button className="button" type="button" disabled={busy || !data || data.total === 0 || nothingUnread} onClick={() => void markAll()}>{busy ? "Отмечаю…" : "Отметить всё прочитанным"}</button>
      </div>
    </div>
    {filtered && data && data.total > FILTER_WINDOW && <p className="notifications-note">Фильтр просматривает последние {FILTER_WINDOW} уведомлений из {data.total}.</p>}
    {actionError && <p className="form-error" role="alert">{actionError}</p>}
    {!data ? (persisted.status === "error" ? <StateView state="unavailable" detail={persisted.message} onRetry={persisted.reload} /> : <StateView state="loading" />) : <div className="notifications-layout"><div className="notifications-main">
      {persisted.status === "error" && <StateView state="unavailable" detail={persisted.message} onRetry={persisted.reload} />}
      {current > 1 && sessionIds.size > 0 && <p className="notifications-note notifications-note--session">В этой сессии пришло уведомлений: {sessionIds.size}. Новые — на первой странице. <button type="button" onClick={() => setPage(1)}>Перейти</button></p>}
      {/* Перезапрос после отметки или события потока не сдвигает ленту: признак обновления стоит в строке заголовка. */}
      <h2 className="list-heading">{filtered ? "Найдено" : "История"} <span>{total}</span>{persisted.status === "loading" && <small className="notifications-refreshing">обновление…</small>}</h2>
      {visible.length ? <div className="notification-list">{visible.map((item) => <NotificationRow key={item.id} item={item} live={sessionIds.has(item.id)} pending={pending.has(item.id)} onRead={() => void markRead(item)} />)}</div> : <StateView state="empty" detail={emptyText} />}
      {total > PAGE_SIZE && <Pager page={current} pageSize={PAGE_SIZE} total={total} onPage={setPage} />}
    </div><aside className="panel notification-help"><Icon name="bell" /><h3>Как это работает</h3><p>Новое уведомление всплывает поверх любого экрана: срочное — на 10 секунд, остальные — на 6,5. Отметка «прочитано» хранится на сервере для вашей учётной записи.</p><dl><div><dt>Всего в истории</dt><dd>{data.total}</dd></div><div><dt>{data.total > data.items.length ? (filtered ? `Непрочитано из последних ${data.items.length}` : "Непрочитано на странице") : "Непрочитано"}</dt><dd>{unreadLoaded}</dd></div><div><dt>Пришло в этой сессии</dt><dd>{events.length}</dd></div></dl></aside></div>}
  </section>;
}

function NotificationRow({ item, live, pending, onRead }: { item: Notification; live: boolean; pending: boolean; onRead: () => void }) {
  const link = linkOf(item.kind, item.payload);
  const group = item.kind === "event.alarm" ? incidentGroupOf(item.payload?.incident_group) : undefined;
  return <article className={`notification-row notification-row--${item.severity}${item.read ? " notification-row--read" : ""}`}><div className="notification-row__icon"><Icon name={item.kind === "workorder.changed" ? "wrench" : item.kind === "alert.new" ? "forecast" : "bell"} /></div><div className="notification-row__body"><div className="notification-row__meta"><span>{KIND_TITLES[item.kind]}</span><time dateTime={item.ts}>{fmtDateTime(item.ts)}</time></div><strong>{link ? <Link to={link} onClick={onRead}>{item.title}</Link> : item.title}{group && <em className="incident-chip">{title("incident_group", group)}</em>}</strong><small>{SEVERITY_TEXT[item.severity]}{!item.read && <> · <b className="notification-row__new">новое</b></>}{live && " · пришло в этой сессии"}</small></div>{!item.read && <button type="button" disabled={pending} onClick={onRead} title="Отметить прочитанным" aria-label={`Отметить прочитанным: ${item.title}`}>✓</button>}</article>;
}
