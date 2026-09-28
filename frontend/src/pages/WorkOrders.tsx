import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Pager, SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime, fmtNumber, fmtWindow, pageParam, scoreText } from "../format";
import { SCENARIOS, title, vocab, type Permission } from "../vocab";

const PAGE_SIZE = 50;
type Order = Schemas["WorkOrderItem"];
type OrderCard = Schemas["WorkOrderCard"];
type Forecast = Schemas["ForecastItem"];
type Status = Order["status"];
type Priority = Order["priority"];
const STATUSES: Status[] = ["draft", "confirmed", "in_progress", "completed", "cancelled"];
const PRIORITIES: Priority[] = ["urgent", "planned", "watch"];
const FILTERS = ["status", "priority", "scenario"] as const;
const FORECASTS_SHOWN = 5;
function statusOf(value: string | null): Status | undefined { return STATUSES.find((item) => item === value); }
function priorityOf(value: string | null): Priority | undefined { return PRIORITIES.find((item) => item === value); }

function plural(count: number, one: string, few: string, many: string): string {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

/** В истории и в «Создал» лежит логин. Служебные логины и демо-логины (они совпадают с кодами ролей) показываем по-русски, остальные — как есть. */
function authorTitle(login: string): string {
  if (login === "system") return "Система";
  if (login === "integration") return "Внешняя система";
  return vocab.roles.find((role) => role.code === login)?.title ?? login;
}

/** Пикеты приходят числами: [174] → «ПК 174», [0, 3, 175.5] → «ПК 0, 3, 175,5». Неразрывный пробел не даёт «ПК» остаться в конце строки. */
function picketsText(pickets: number[]): string {
  return `ПК\u00a0${pickets.map((value) => fmtNumber(value)).join(", ")}`;
}

export function WorkOrders() {
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string | null>(params.get("open"));
  const page = pageParam(params.get("page")), status = statusOf(params.get("status")), priority = priorityOf(params.get("priority")), scenario = SCENARIOS.find((s) => s.code === params.get("scenario"))?.code;
  const filtered = Boolean(status || priority || scenario);
  const load = useLoad(() => api.GET("/api/v1/work-orders", { params: { query: { status, priority, scenario, page, page_size: PAGE_SIZE } } }), [status, priority, scenario, page]);
  useEffect(() => { setSelected(params.get("open")); }, [params]);
  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (!value || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }
  function resetFilters() { const next = new URLSearchParams(params); for (const key of [...FILTERS, "page"]) next.delete(key); setParams(next); }
  function openOrder(id: string) { const next = new URLSearchParams(params); next.set("open", id); setParams(next); }
  function closeOrder() { const next = new URLSearchParams(params); next.delete("open"); setParams(next, { replace: true }); }
  return <section>
    <PageHeader eyebrow="Превентивное обслуживание" title="Заявки на работы" description="Путь от прогноза до подтверждённого выполнения работ" />
    <div className="filter-panel orders-filters"><label className="field"><span>Статус</span><select value={status ?? ""} onChange={(e) => update("status", e.target.value)}><option value="">Все статусы</option>{STATUSES.map((item) => <option key={item} value={item}>{title("work_order_status", item)}</option>)}</select></label><label className="field"><span>Приоритет</span><select value={priority ?? ""} onChange={(e) => update("priority", e.target.value)}><option value="">Все приоритеты</option>{PRIORITIES.map((item) => <option key={item} value={item}>{title("work_order_priority", item)}</option>)}</select></label><label className="field"><span>Сценарий</span><select value={scenario ?? ""} onChange={(e) => update("scenario", e.target.value)} title={scenario ? title("scenario", scenario) : undefined}><option value="">Все сценарии</option>{SCENARIOS.map((item) => <option key={item.code} value={item.code}>{item.title}</option>)}</select></label></div>
    {/* «На странице» не показываем: это же число уже есть в строке страниц внизу. */}
    <Loaded load={load}>{(data) => <><div className="section-summary"><span className="summary-pill">{filtered ? "Найдено" : "Всего заявок"} <strong>{data.total}</strong></span>{filtered && <button type="button" className="button orders-reset" onClick={resetFilters}>Сбросить фильтры</button>}</div>{data.items.length === 0 ? <StateView state="empty" detail={filtered ? "Под выбранные фильтры заявок нет." : "Черновики заявок появляются после ежедневного расчёта."} /> : <OrderTable items={data.items} onOpen={openOrder} />}<Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(value) => update("page", value)} /></>}</Loaded>
    {selected && <OrderDrawer id={selected} onClose={closeOrder} onChanged={load.reload} />}
  </section>;
}

function OrderTable({ items, onOpen }: { items: Order[]; onOpen: (id: string) => void }) {
  return <div className="orders-grid">{items.map((order) => <button type="button" className="order-card" key={order.id} onClick={() => onOpen(order.id)}><div className="order-card__top"><span className={`priority priority--${order.priority}`}>{title("work_order_priority", order.priority)}</span><span className={`order-status order-status--${order.status}`}>{title("work_order_status", order.status)}</span></div><strong>{order.id}</strong><h3>{order.work_type}</h3><p>{order.object.name ?? order.object.id ?? "Объект не указан"}</p><div className="order-card__meta"><span title="Срок выполнения"><Icon name="calendar" />до {fmtDateTime(order.due_by)}</span><span><Icon name="forecast" />{order.forecast_ids.length} {plural(order.forecast_ids.length, "прогноз", "прогноза", "прогнозов")}</span></div><div className="order-card__footer"><span>{title("scenario", order.scenario)}</span><Icon name="arrow" /></div></button>)}</div>;
}

function OrderDrawer({ id, onClose, onChanged }: { id: string; onClose: () => void; onChanged: () => void }) {
  const { can } = useAuth();
  const load = useLoad(() => api.GET("/api/v1/work-orders/{order_id}", { params: { path: { order_id: id } } }), [id]);
  const [reason, setReason] = useState(""); const [message, setMessage] = useState<string | null>(null); const [success, setSuccess] = useState<string | null>(null); const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const successRef = useRef<HTMLParagraphElement>(null);
  const messageRef = useRef<HTMLParagraphElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  // Кнопки перехода внизу панели, а ответ — вверху, рядом со статусом: переводим фокус, иначе на телефоне ответ остаётся за краем экрана.
  useEffect(() => { if (success) successRef.current?.focus(); }, [success]);
  useEffect(() => { if (message) messageRef.current?.focus(); }, [message]);
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
  async function transition(current: Status, next: Status) {
    setBusy(true); setMessage(null); setSuccess(null);
    try {
      const { data, error, response } = await api.PATCH("/api/v1/work-orders/{order_id}", { params: { path: { order_id: id } }, body: { expected_status: current, status: next, reason: reason.trim() || null } });
      if (data && response.ok) {
        load.reload(); onChanged(); setReason("");
        setSuccess(`Статус изменён: ${title("work_order_status", next)}`);
      } else if (response.status === 409) {
        // Новый статус мог убрать все переходы; сообщение держим вне блока действий.
        // Backend кладёт в 409 текущий статус — называем его, чтобы было ясно, что изменилось.
        const actual = (error as { detail?: { current_status?: string } } | undefined)?.detail?.current_status;
        setMessage(`Заявку уже изменил другой пользователь${actual ? `: сейчас она в статусе «${title("work_order_status", actual)}»` : ""}. Карточка обновлена — проверьте, нужно ли ещё действие.`);
        load.reload(); onChanged();
      } else setMessage(errorText(error, response));
    } catch { setMessage(errorText(null, undefined)); }
    finally { setBusy(false); }
  }
  return <div className="drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}><aside ref={dialogRef} className="order-drawer" role="dialog" aria-modal="true" aria-label={`Заявка ${id}`}><button ref={closeRef} className="drawer-close" type="button" onClick={onClose} aria-label="Закрыть карточку">×</button><Loaded load={load}>{(order) => {
    const available = (vocab.work_order_transitions[order.status] ?? []) as Status[];
    const permitted = available.filter((next) => can(vocab.work_order_transition_perm[next] as Permission));
    const pickets = order.pickets ?? [];
    const place = pickets.length ? picketsText(pickets) : null;
    return <><span className="panel__eyebrow">Карточка заявки</span><h2>{order.id} <SourceBadge source={order.source} /></h2><div className="drawer-tags"><span className={`priority priority--${order.priority}`}>{title("work_order_priority", order.priority)}</span><span className={`order-status order-status--${order.status}`}>{title("work_order_status", order.status)}</span></div>{success && <p ref={successRef} className="form-ok drawer-feedback" role="status" tabIndex={-1}>{success}</p>}{message && <p ref={messageRef} className="form-error drawer-feedback drawer-feedback--error" role="alert" tabIndex={-1}>{message}</p>}<h3>{order.work_type}</h3><p className="drawer-object">{order.object.name ?? order.object.id ?? "—"}</p>
      <dl className="drawer-fields"><div><dt>Срок</dt><dd>до {fmtDateTime(order.due_by)}</dd></div><div><dt>Сценарий</dt><dd>{title("scenario", order.scenario)}</dd></div>{place && <div><dt>{pickets.length > 1 ? "Пикеты" : "Пикет"}</dt><dd>{place}</dd></div>}<div><dt>Создал</dt><dd>{authorTitle(order.created_by)}, {fmtDateTime(order.created_at)}</dd></div></dl>
      {order.rationale?.length ? <div className="rationale"><strong>Что повлияло на прогноз</strong><ul>{order.rationale.map((item) => <li key={item}>{item}</li>)}</ul></div> : null}
      {order.forecast_ids.length > 0 && <LinkedForecasts key={order.id} order={order} />}
      {permitted.length > 0 && <div className="transition-box"><strong>Следующее действие</strong><textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} aria-label="Причина или комментарий к переходу" placeholder="Причина или комментарий — попадёт в историю заявки" maxLength={2000} disabled={load.status !== "ok" || busy} /><div>{permitted.map((next) => <button key={next} type="button" className={`button ${next !== "cancelled" ? "button--primary" : ""}`} disabled={busy || load.status !== "ok"} onClick={() => void transition(order.status, next)}>{TRANSITION_VERB[next] ?? title("work_order_status", next)}</button>)}</div></div>}
      {/* У роли нет прав на следующий шаг: пустая форма без кнопок только сбивает, поэтому говорим, кто этот шаг делает. */}
      {available.length > 0 && permitted.length === 0 && <div className="transition-box transition-box--readonly"><strong>Следующее действие</strong><p>{whoActs(available)}</p></div>}
      <h3 className="history-title">История</h3><ol className="order-history">{(order.history ?? []).map((item, index) => <li key={`${item.at}-${index}`}><i /><div><strong>{title("work_order_status", item.to_status)}</strong><span>{fmtDateTime(item.at)} · {authorTitle(item.author)}</span>{item.reason && <p>{item.reason}</p>}</div></li>)}</ol></>; }}</Loaded></aside></div>;
}

/** Связанные прогнозы: вместо шестнадцатеричных id — место, окно и оценка. Один запрос по объекту и сценарию заявки; не ответил — остаются id. */
function LinkedForecasts({ order }: { order: OrderCard }) {
  const obj = order.object.id ?? undefined;
  const load = useLoad(() => api.GET("/api/v1/forecasts", { params: { query: { obj, scenario: order.scenario, page_size: 500 } } }), [order.id, obj, order.scenario]);
  const found = new Map<string, Forecast>((load.data?.items ?? []).map((item) => [item.id, item]));
  const pending = load.status === "loading" && load.data === undefined;
  const count = order.forecast_ids.length;
  // У заявки бывает до 20 прогнозов: полный список уводил «Следующее действие» на три экрана вниз.
  const [expanded, setExpanded] = useState(false);
  const collapsed = count > FORECASTS_SHOWN + 1 && !expanded;
  const ids = collapsed ? order.forecast_ids.slice(0, FORECASTS_SHOWN) : order.forecast_ids;
  return <div className="drawer-forecasts"><strong>{count > 1 ? `Связанные прогнозы: ${count}` : "Связанный прогноз"}</strong><div>{ids.map((forecastId) => { const item = found.get(forecastId); return <Link key={forecastId} to={`/forecasts/${encodeURIComponent(forecastId)}`}><Icon name="forecast" /><span><b>{item ? forecastPlace(item) : pending ? "Прогноз" : `Прогноз ${forecastId}`}</b><small>{item ? `${fmtWindow(item.valid_from, item.valid_to)} · ${scoreText(item)}` : pending ? "загрузка…" : "открыть карточку прогноза"}</small></span><Icon name="arrow" /></Link>; })}</div>{count > FORECASTS_SHOWN + 1 && <button type="button" className="drawer-forecasts__more" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? "Свернуть список" : `Показать ещё ${count - FORECASTS_SHOWN}`}</button>}</div>;
}

function forecastPlace(item: Forecast): string {
  const channel = item.channel;
  if (!channel) return title("kind", item.kind);
  return [channel.picket_label, channel.name?.trim() || `канал ${channel.id}`].filter(Boolean).join(" · ");
}

/** «Подтвердить или отменить заявку могут: диспетчер ОДС, …» — роли из матрицы прав, без внешней системы. */
function whoActs(available: Status[]): string {
  const roles = new Set<string>();
  for (const next of available) for (const role of vocab.permissions[vocab.work_order_transition_perm[next]] ?? []) if (role !== "integration") roles.add(role);
  const verbs = available.map((next, index) => { const verb = TRANSITION_VERB[next] ?? title("work_order_status", next); return index === 0 ? verb : verb.toLowerCase(); }).join(" или ");
  const who = vocab.roles.filter((role) => roles.has(role.code)).map((role) => role.title.charAt(0).toLowerCase() + role.title.slice(1)).join(", ");
  return `${verbs} могут: ${who}.`;
}

/** Кнопка называет действие, а не будущий статус. «Отменить» без дополнения читается как «закрыть форму», поэтому — «Отменить заявку». */
const TRANSITION_VERB: Partial<Record<Status, string>> = {
  confirmed: "Подтвердить", in_progress: "Взять в работу", completed: "Отметить выполненной", cancelled: "Отменить заявку",
};
