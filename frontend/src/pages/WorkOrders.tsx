import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Pager, SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime, pageParam } from "../format";
import { SCENARIOS, title, vocab } from "../vocab";

const PAGE_SIZE = 50;
type Order = Schemas["WorkOrderItem"];
type Status = Order["status"];
type Priority = Order["priority"];
const STATUSES: Status[] = ["draft", "confirmed", "in_progress", "completed", "cancelled"];
const PRIORITIES: Priority[] = ["urgent", "planned", "watch"];
function statusOf(value: string | null): Status | undefined { return STATUSES.find((item) => item === value); }
function priorityOf(value: string | null): Priority | undefined { return PRIORITIES.find((item) => item === value); }

export function WorkOrders() {
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string | null>(params.get("open"));
  const page = pageParam(params.get("page")), status = statusOf(params.get("status")), priority = priorityOf(params.get("priority")), scenario = SCENARIOS.find((s) => s.code === params.get("scenario"))?.code;
  const load = useLoad(() => api.GET("/api/v1/work-orders", { params: { query: { status, priority, scenario, page, page_size: PAGE_SIZE } } }), [status, priority, scenario, page]);
  useEffect(() => { setSelected(params.get("open")); }, [params]);
  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (!value || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }
  function openOrder(id: string) { const next = new URLSearchParams(params); next.set("open", id); setParams(next); }
  function closeOrder() { const next = new URLSearchParams(params); next.delete("open"); setParams(next, { replace: true }); }
  return <section>
    <PageHeader eyebrow="Превентивное обслуживание" title="Заявки на работы" description="Путь от прогноза до подтверждённого выполнения работ" />
    <div className="filter-panel orders-filters"><label className="field"><span>Статус</span><select value={status ?? ""} onChange={(e) => update("status", e.target.value)}><option value="">Все статусы</option>{STATUSES.map((item) => <option key={item} value={item}>{title("work_order_status", item)}</option>)}</select></label><label className="field"><span>Приоритет</span><select value={priority ?? ""} onChange={(e) => update("priority", e.target.value)}><option value="">Все приоритеты</option>{PRIORITIES.map((item) => <option key={item} value={item}>{title("work_order_priority", item)}</option>)}</select></label><label className="field"><span>Сценарий</span><select value={scenario ?? ""} onChange={(e) => update("scenario", e.target.value)}><option value="">Все сценарии</option>{SCENARIOS.map((item) => <option key={item.code} value={item.code}>{item.title}</option>)}</select></label></div>
    <Loaded load={load}>{(data) => <><div className="section-summary"><span className="summary-pill">Всего заявок <strong>{data.total}</strong></span><span className="summary-pill">На странице <strong>{data.items.length}</strong></span></div>{data.items.length === 0 ? <StateView state="empty" /> : <OrderTable items={data.items} onOpen={openOrder} />}<Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(value) => update("page", value)} /></>}</Loaded>
    {selected && <OrderDrawer id={selected} onClose={closeOrder} onChanged={load.reload} />}
  </section>;
}

function OrderTable({ items, onOpen }: { items: Order[]; onOpen: (id: string) => void }) {
  return <div className="orders-grid">{items.map((order) => <button type="button" className="order-card" key={order.id} onClick={() => onOpen(order.id)}><div className="order-card__top"><span className={`priority priority--${order.priority}`}>{title("work_order_priority", order.priority)}</span><span className={`order-status order-status--${order.status}`}>{title("work_order_status", order.status)}</span></div><strong>{order.id}</strong><h3>{order.work_type}</h3><p>{order.object.name ?? order.object.id ?? "Объект не указан"}</p><div className="order-card__meta"><span><Icon name="calendar" />{fmtDateTime(order.due_by)}</span><span><Icon name="forecast" />{order.forecast_ids.length} прогнозов</span></div><div className="order-card__footer"><span>{title("scenario", order.scenario)}</span><Icon name="arrow" /></div></button>)}</div>;
}

function OrderDrawer({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const { can } = useAuth();
  const load = useLoad(() => api.GET("/api/v1/work-orders/{order_id}", { params: { path: { order_id: id } } }), [id]);
  const [reason, setReason] = useState(""); const [message, setMessage] = useState<string | null>(null); const [success, setSuccess] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const successRef = useRef<HTMLParagraphElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => { if (success) successRef.current?.focus(); }, [success]);
  useEffect(() => {
    const previous = document.body.style.overflow; document.body.style.overflow = "hidden";
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setMessage(null); setSuccess(null); setReason("");
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") { event.preventDefault(); onCloseRef.current(); return; }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled])') ?? []).filter((element) => element.getClientRects().length > 0);
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!first || !last) { event.preventDefault(); return; }
      if (!dialogRef.current?.contains(document.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); }
      else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", onKeyDown); if (previousFocus?.isConnected) previousFocus.focus(); else document.querySelector<HTMLElement>(".orders-filters select")?.focus(); };
  }, [id]);
  async function transition(current: Status, next: Status) { setBusy(true); setMessage(null); setSuccess(null); try { const { data, error, response } = await api.PATCH("/api/v1/work-orders/{order_id}", { params: { path: { order_id: id } }, body: { expected_status: current, status: next, reason: reason.trim() || null } }); if (data) { load.reload(); onChanged(); setReason(""); setSuccess(`Статус изменён: ${title("work_order_status", next)}`); } else setMessage(errorText(error, response)); } catch { setMessage(errorText(null, undefined)); } finally { setBusy(false); } }
  return <div className="drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}><aside ref={dialogRef} className="order-drawer" role="dialog" aria-modal="true" aria-label={`Заявка ${id}`}><button ref={closeRef} className="drawer-close" type="button" onClick={onClose} aria-label="Закрыть карточку">×</button><Loaded load={load}>{(order) => { const available = (vocab.work_order_transitions[order.status] ?? []) as Status[]; return <><span className="panel__eyebrow">Карточка заявки</span><h2>{order.id} <SourceBadge source={order.source} /></h2><div className="drawer-tags"><span className={`priority priority--${order.priority}`}>{title("work_order_priority", order.priority)}</span><span className={`order-status order-status--${order.status}`}>{title("work_order_status", order.status)}</span></div>{success && <p ref={successRef} className="form-ok drawer-feedback" role="status" tabIndex={-1}>{success}</p>}<h3>{order.work_type}</h3><p className="drawer-object">{order.object.name ?? order.object.id ?? "—"}</p><dl className="drawer-fields"><div><dt>Срок</dt><dd>{fmtDateTime(order.due_by)}</dd></div><div><dt>Сценарий</dt><dd>{title("scenario", order.scenario)}</dd></div><div><dt>Создал</dt><dd>{order.created_by}, {fmtDateTime(order.created_at)}</dd></div><div><dt>Прогнозов</dt><dd>{order.forecast_ids.length}</dd></div></dl>{order.rationale?.length ? <div className="rationale"><strong>Основания</strong>{order.rationale.map((item) => <p key={item}>{item}</p>)}</div> : null}
        {order.forecast_ids.length > 0 && <div className="drawer-forecasts"><strong>Связанные прогнозы</strong><div>{order.forecast_ids.map((forecastId) => <Link key={forecastId} to={`/forecasts/${encodeURIComponent(forecastId)}`}><Icon name="forecast" /><span>{forecastId}</span><Icon name="arrow" /></Link>)}</div></div>}
        {available.length > 0 && <div className="transition-box"><strong>Следующее действие</strong><textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Комментарий к изменению статуса" />{message && <p className="form-error" role="alert">{message}</p>}<div>{available.filter((next) => next === "in_progress" || next === "completed" ? can("work_order_progress") : can("work_order_manage")).map((next) => <button key={next} className={`button ${next !== "cancelled" ? "button--primary" : ""}`} disabled={busy} onClick={() => void transition(order.status, next)}>{title("work_order_status", next)}</button>)}</div></div>}
        <h3 className="history-title">История</h3><ol className="order-history">{(order.history ?? []).map((item, index) => <li key={`${item.at}-${index}`}><i /><div><strong>{title("work_order_status", item.to_status)}</strong><span>{fmtDateTime(item.at)} · {item.author}</span>{item.reason && <p>{item.reason}</p>}</div></li>)}</ol></>; }}</Loaded></aside></div>;
}
