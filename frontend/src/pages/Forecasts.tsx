import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Pager, SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtNumber, fmtWindow, pageParam, placeText } from "../format";
import { ACTIONS, SCENARIOS, SCENARIO_SHORT, title, type Scenario } from "../vocab";

const PAGE_SIZE = 50;
const FILTER_KEYS = ["scenario", "from", "to", "decision", "outcome", "obj", "group_by"] as const;
type Forecast = Schemas["ForecastItem"];
type Week = Schemas["ForecastWeek"];
interface ObjectOption { id: string; name: string }

// Вероятность всегда с двумя знаками: «0,90» и «1,00» стоят в столбце ровно, рядом с «0,93».
const PROBABILITY = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
// В журнале решение — уже принятое состояние: «Отклонить» в колонке читается как кнопка.
// Полная подпись словаря остаётся во всплывающей подсказке чипа.
const DECISION_LABEL: Record<string, string> = { remote_check: "Удалённая проверка", defer: "Отложено", reject: "Отклонено" };
// «по данным СМВУ» вынесено в заголовок колонки, иначе факт не помещается в строку на ноутбуке 1280 px.
const OUTCOME_SHORT: Record<string, string> = { hit: "Наступило", miss: "Не наступило", unknown: "Неизвестно" };
const PENDING_HINT = "Факт по данным СМВУ появится после закрытия окна прогноза";

function scenarioParam(value: string | null): Scenario | undefined { return SCENARIOS.find((s) => s.code === value)?.code; }
function oneOf<T extends string>(value: string | null, values: readonly T[]): T | undefined { return values.find((item) => item === value); }
function objectKey(item: Forecast): string { return item.object.id ?? item.object.name ?? `unknown:${item.id}`; }
function decisionLabel(code: string): string { return DECISION_LABEL[code] ?? title("action", code); }
function scoreValue(item: Forecast): string { if (item.score_type !== "probability") return fmtNumber(item.priority_score); return item.risk === null || item.risk === undefined ? "—" : PROBABILITY.format(item.risk); }

/** Понедельник недели строки, как его считает backend. Дата без часового пояса браузера: asof — день по МСК. */
function weekStart(asof: string): string {
  const [y, m, d] = asof.split("-").map(Number);
  const day = new Date(Date.UTC(y, m - 1, d));
  day.setUTCDate(d - (day.getUTCDay() + 6) % 7);
  return day.toISOString().slice(0, 10);
}

/** «22.06–28.06»: неделя с понедельника по воскресенье. */
function weekRange(start: string): string {
  const [y, m, d] = start.split("-").map(Number);
  const pad = (n: number) => String(n).padStart(2, "0");
  const end = new Date(Date.UTC(y, m - 1, d + 6));
  return `${pad(d)}.${pad(m)}–${pad(end.getUTCDate())}.${pad(end.getUTCMonth() + 1)}`;
}

/** Выгрузка журнала за период фильтра: GET под той же cookie, файл отдаёт сервер. */
function exportHref(from?: string, to?: string): string {
  const query = new URLSearchParams();
  if (from) query.set("from", from);
  if (to) query.set("to", to);
  const tail = query.toString();
  return `/api/v1/export/forecasts.xlsx${tail ? `?${tail}` : ""}`;
}

/**
 * Backend фильтрует журнал по точному id объекта, а диспетчер знает название. Название
 * сводим к id по объектам самого журнала: совпадение id или названия, иначе единственное
 * вхождение подстроки. Неоднозначный текст уходит как есть и честно даёт пустой список.
 */
function resolveObject(text: string, options: ObjectOption[]): string {
  const value = text.trim(), low = value.toLowerCase();
  if (!value) return "";
  const exact = options.find((o) => o.id === value || o.name.toLowerCase() === low);
  if (exact) return exact.id;
  const partial = options.filter((o) => o.name.toLowerCase().includes(low));
  return partial.length === 1 ? partial[0].id : value;
}

export function Forecasts() {
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<Forecast[]>([]);
  const [creating, setCreating] = useState(false);
  const [createdOrder, setCreatedOrder] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const scenario = scenarioParam(params.get("scenario"));
  const page = pageParam(params.get("page"));
  const from = params.get("from") || undefined;
  const to = params.get("to") || undefined;
  const obj = params.get("obj") || undefined;
  const decision = oneOf(params.get("decision"), ["none", "any", "dispatch_crew", "remote_check", "defer", "reject"] as const);
  const outcome = oneOf(params.get("outcome"), ["hit", "miss", "unknown"] as const);
  const groupBy = oneOf(params.get("group_by"), ["obj", "case_key"] as const);
  const activeFilters = FILTER_KEYS.filter((key) => params.get(key)).length;
  const load = useLoad(() => api.GET("/api/v1/forecasts", { params: { query: { scenario, from, to, decision, outcome, obj, group_by: groupBy, page, page_size: PAGE_SIZE } } }), [scenario, from, to, decision, outcome, obj, groupBy, page]);
  // Итог не зависит от страницы: те же фильтры, все строки. Листание его не перезапрашивает.
  const summary = useLoad(() => api.GET("/api/v1/forecasts/summary", { params: { query: { scenario, from, to, decision, outcome, obj, group_by: groupBy } } }), [scenario, from, to, decision, outcome, obj, groupBy]);
  function reload() { load.reload(); summary.reload(); }
  // Подсказки поля «Объект»: по одному последнему прогнозу на объект — это ровно объекты журнала.
  const objects = useLoad(() => api.GET("/api/v1/forecasts", { params: { query: { group_by: "obj", page_size: 500 } } }), []);
  const objectOptions = useMemo<ObjectOption[]>(() => (objects.data?.items ?? []).flatMap((i) => i.object.id ? [{ id: i.object.id, name: i.object.name ?? i.object.id }] : []).sort((a, b) => a.name.localeCompare(b.name, "ru")), [objects.data]);
  const objName = obj ? objectOptions.find((o) => o.id === obj)?.name : undefined;
  // Текст, который не свёлся к одному объекту: либо такого нет, либо он подходит к нескольким.
  const objMatches = obj && objects.data && !objName ? objectOptions.filter((o) => o.name.toLowerCase().includes(obj.toLowerCase())) : [];

  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (value === undefined || value === "" || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }
  function reset() { setParams({}); }
  function applyObject(input: HTMLInputElement) { const value = resolveObject(input.value, objectOptions); const known = objectOptions.find((o) => o.id === value); if (known) input.value = known.name; if (value !== (obj ?? "")) update("obj", value); }
  function toggle(item: Forecast) { setCreatedOrder(null); setCreateError(null); setSelected((items) => items.some((value) => value.id === item.id) ? items.filter((value) => value.id !== item.id) : [...items, item]); }
  function compatible(item: Forecast): boolean { const first = selected[0]; return !first || (first.scenario === item.scenario && objectKey(first) === objectKey(item)); }
  async function createOrder() {
    if (!selected.length) return; setCreating(true); setCreateError(null);
    try { const { data, error, response } = await api.POST("/api/v1/work-orders", { body: { forecast_ids: selected.map((item) => item.id) } }); if (data) { setCreatedOrder(data.id); setSelected([]); reload(); } else setCreateError(errorText(error, response)); }
    catch { setCreateError(errorText(null, undefined)); } finally { setCreating(false); }
  }
  const countLabel = groupBy === "obj" ? "Объектов" : groupBy === "case_key" ? "Случаев" : "Найдено";
  const emptyDetail = obj && objects.data && !objName ? (objMatches.length > 1 ? `«${obj}» подходит к нескольким объектам: ${objMatches.slice(0, 5).map((o) => o.name).join(", ")}${objMatches.length > 5 ? " и другим" : ""}. Выберите один в подсказках поля «Объект».` : `Объекта «${obj}» нет в журнале. Выберите название из подсказок поля «Объект».`) : activeFilters ? "Под выбранные условия прогнозов нет. Измените фильтры или нажмите «Сбросить»." : undefined;
  return <section>
    <PageHeader eyebrow="Предиктивная аналитика" title="Журнал прогнозов" description="Единая очередь рисков с решениями диспетчера и результатами проверки" actions={<>{can("export") && <a className="button" href={exportHref(from, to)} download title="Весь журнал за период из полей «Дата от» и «Дата до»; остальные фильтры в файл не переносятся"><Icon name="download" /> Экспорт Excel</a>}<button className="button" type="button" onClick={reload}>Обновить</button></>} />
    {/* На телефоне семь полей занимают экран целиком, поэтому там они свёрнуты за кнопкой. */}
    <button type="button" className="button filter-toggle" aria-expanded={filtersOpen} aria-controls="forecast-filters" onClick={() => setFiltersOpen((open) => !open)}><Icon name="filter" /> Фильтры{activeFilters ? <b>{activeFilters}</b> : null}</button>
    <div id="forecast-filters" className={`filter-panel forecast-filters${filtersOpen ? " forecast-filters--open" : ""}`}>
      <label className="field"><span>Сценарий</span><select value={scenario ?? ""} onChange={(e) => update("scenario", e.target.value)}><option value="">Все сценарии</option>{SCENARIOS.map((s) => <option key={s.code} value={s.code}>{s.title}</option>)}</select></label>
      <label className="field"><span>Объект</span><input key={`${obj ?? ""}:${objName ?? ""}`} defaultValue={objName ?? obj ?? ""} list="forecast-objects" onBlur={(e) => applyObject(e.currentTarget)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); applyObject(e.currentTarget); } }} placeholder="Название или ID" title="Enter — применить" /><datalist id="forecast-objects">{objectOptions.map((o) => <option key={o.id} value={o.name} />)}</datalist></label>
      <label className="field"><span>Дата от</span><input type="date" value={from ?? ""} max={to} onChange={(e) => update("from", e.target.value)} /></label>
      <label className="field"><span>Дата до</span><input type="date" value={to ?? ""} min={from} onChange={(e) => update("to", e.target.value)} /></label>
      <label className="field"><span>Решение</span><select value={decision ?? ""} onChange={(e) => update("decision", e.target.value)}><option value="">Все решения</option><option value="none">Без решения</option><option value="any">Решение принято</option>{ACTIONS.map((a) => <option key={a.code} value={a.code}>{decisionLabel(a.code)}</option>)}</select></label>
      <label className="field"><span>Факт</span><select value={outcome ?? ""} onChange={(e) => update("outcome", e.target.value)}><option value="">Любой</option><option value="hit">Попадание</option><option value="miss">Промах</option><option value="unknown">Неизвестно</option></select></label>
      <label className="field"><span>Группировка</span><select value={groupBy ?? ""} onChange={(e) => update("group_by", e.target.value)}><option value="">Без группировки</option><option value="obj">По объекту</option><option value="case_key">По случаю</option></select></label>
      <button type="button" className="button filter-reset" disabled={!activeFilters && page === 1} onClick={reset}><Icon name="filter" /> Сбросить</button>
    </div>
    <Loaded load={load}>{(data) => <>
      {data.total > 0 && <div className="section-summary forecast-summary"><span className="summary-pill">{countLabel} <strong>{data.total}</strong></span>{groupBy && <span className="summary-note">по каждому {groupBy === "obj" ? "объекту" : "случаю"} показан последний прогноз</span>}</div>}
      {createdOrder && <div className="action-success"><Icon name="orders" /><span>Черновик <strong>{createdOrder}</strong> сформирован</span><Link to={`/work-orders?open=${encodeURIComponent(createdOrder)}`}>Открыть заявку</Link></div>}
      {createError && <div className="state state--bad">{createError}</div>}
      {can("work_order_manage") && selected.length > 0 && <div className="bulk-bar"><div><strong>Выбрано: {selected.length}</strong><span>{selected[0].object.name ?? selected[0].object.id} · {selected[0].scenario_title}</span></div><button className="button" type="button" onClick={() => setSelected([])}>Отменить</button><button className="button button--primary" type="button" disabled={creating} onClick={() => void createOrder()}>{creating ? "Формирование…" : "Создать общую заявку"}</button></div>}
      {data.items.length === 0 ? <StateView state="empty" detail={emptyDetail} /> : <ForecastTable items={data.items} selectable={can("work_order_manage")} selected={selected} compatible={compatible} onToggle={toggle} />}
      <WeekTotals items={data.items} weeks={summary.data?.weeks} />
      <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(value) => update("page", value)} />
    </>}</Loaded>
  </section>;
}

// Итог — по неделям, чьи строки видны на странице, но посчитан сервером по всему фильтру: листаемая неделя сверяется со своим полным итогом.
function WeekTotals({ items, weeks }: { items: Forecast[]; weeks?: Week[] }) {
  const byStart = new Map((weeks ?? []).map((week) => [week.week_start, week]));
  const shown = [...new Set(items.map((item) => weekStart(item.asof)))].flatMap((start) => byStart.get(start) ?? []);
  if (!shown.length) return null;
  return <div className="week-totals" aria-label="Итог по неделям этой страницы">
    {shown.map((week) => <div key={week.week_start} className="week-total"><strong>Неделя {weekRange(week.week_start)}</strong><span>выдано <b>{fmtNumber(week.issued)}</b></span><span className="outcome outcome--hit">попало <b>{fmtNumber(week.hit)}</b></span><span className="outcome outcome--miss">промахов <b>{fmtNumber(week.miss)}</b></span><span className="outcome outcome--unknown">неизвестно <b>{fmtNumber(week.unknown)}</b></span><span title="Прогнозы, по которым диспетчер принял решение">решено <b>{fmtNumber(week.decided)}</b></span></div>)}
    <p className="week-totals__note">По всем строкам журнала за неделю при выбранных фильтрах, не только по этой странице. «Неизвестно» включает прогнозы с незакрытым окном — так же считает экран «Качество».</p>
  </div>;
}

function ForecastTable({ items, selectable, selected, compatible, onToggle }: { items: Forecast[]; selectable: boolean; selected: Forecast[]; compatible: (item: Forecast) => boolean; onToggle: (item: Forecast) => void }) {
  // Первая колонка — связь с заявкой: ссылка на уже созданную или отметка для новой общей.
  const lead = selectable || items.some((item) => item.work_order_id);
  return <div className="table-wrap"><table className="table forecast-table"><thead><tr>{lead && <th className="select-cell"><span className="sr-only">Заявка</span></th>}<th className="col-rank" title="Место в очереди дня">№</th><th className="col-when">Прогноз и окно</th><th className="col-scenario">Направление</th><th className="col-object">Объект</th><th className="col-score">Оценка</th><th className="col-decision">Решение</th><th className="col-fact" title="Факт по данным СМВУ и итог проверки на месте">Факт СМВУ · итог</th><th className="col-go"><span className="sr-only">Карточка</span></th></tr></thead><tbody>{items.map((item) => { const checked = selected.some((value) => value.id === item.id); const disabled = !checked && !compatible(item); const sub = [item.channel?.picket_label, item.channel?.name].filter(Boolean).join(" · ") || "объект целиком"; return <tr key={item.id} className={checked ? "forecast-row--selected" : ""}>
    {lead && <td className="select-cell">{item.work_order_id ? <Link className="order-mark" to={`/work-orders?open=${encodeURIComponent(item.work_order_id)}`} title={`Заявка ${item.work_order_id}`} aria-label={`Открыть заявку ${item.work_order_id}`}><Icon name="orders" /></Link> : selectable ? <input type="checkbox" checked={checked} disabled={disabled} onChange={() => onToggle(item)} aria-label={`Выбрать прогноз ${item.id}`} title={disabled ? "Для общей заявки выберите тот же объект и сценарий" : "Добавить в общую заявку"} /> : null}</td>}
    <td className="col-rank"><span className={`rank-badge ${item.rank <= 3 ? "rank-badge--hot" : ""}`}>{item.rank}</span></td>
    <td className="col-when"><strong>{fmtDate(item.asof)}</strong><small className="cell-sub">{fmtWindow(item.valid_from, item.valid_to)}</small></td>
    <td className="col-scenario"><span className={`scenario-tag scenario-tag--${item.scenario}`} title={item.scenario_title}>{SCENARIO_SHORT[item.scenario] ?? item.scenario_title}</span><small className="cell-sub">горизонт {item.horizon_hours} ч</small></td>
    <td className="col-object"><Link className="object-link" to={`/forecasts/${encodeURIComponent(item.id)}`} title={placeText(item)}>{item.object.name ?? item.object.id ?? "—"}</Link><small className="cell-sub object-sub"><span title={sub}>{sub}</span><SourceBadge source={item.source} /></small></td>
    <td className="col-score"><strong>{scoreValue(item)}</strong><small className="cell-sub">{item.score_type === "probability" ? "вероятность" : "приоритет"}</small></td>
    <td className="col-decision" data-label="Решение">{item.decision ? <span className="chip chip--info" title={title("action", item.decision.action)}>{decisionLabel(item.decision.action)}</span> : <span className="chip chip--muted">Не принято</span>}</td>
    <td className="col-fact" data-label="Факт СМВУ · итог"><span className={`outcome outcome--${item.outcome_auto ?? "pending"}`} title={item.outcome_auto ? title("outcome_auto", item.outcome_auto) : PENDING_HINT}>{item.outcome_auto ? OUTCOME_SHORT[item.outcome_auto] ?? title("outcome_auto", item.outcome_auto) : "Ожидается"}</span><small className="cell-sub" title="Итог проверки на месте">итог: {item.outcome_manual ? title("outcome_manual", item.outcome_manual) : "не внесён"}</small></td>
    <td className="col-go"><Link className="row-arrow" to={`/forecasts/${encodeURIComponent(item.id)}`} aria-label={`Карточка прогноза: ${item.object.name ?? item.object.id ?? item.id}`}><Icon name="arrow" /></Link></td>
  </tr>; })}</tbody></table></div>;
}
