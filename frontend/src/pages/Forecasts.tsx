import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Pager, SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtDateTime, pageParam, placeText, scoreText } from "../format";
import { ACTIONS, SCENARIOS, title, type Scenario } from "../vocab";

const PAGE_SIZE = 50;
type Forecast = Schemas["ForecastItem"];

function scenarioParam(value: string | null): Scenario | undefined { return SCENARIOS.find((s) => s.code === value)?.code; }
function oneOf<T extends string>(value: string | null, values: readonly T[]): T | undefined { return values.find((item) => item === value); }
function objectKey(item: Forecast): string { return item.object.id ?? item.object.name ?? `unknown:${item.id}`; }

export function Forecasts() {
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<Forecast[]>([]);
  const [creating, setCreating] = useState(false);
  const [createdOrder, setCreatedOrder] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const scenario = scenarioParam(params.get("scenario"));
  const page = pageParam(params.get("page"));
  const from = params.get("from") || undefined;
  const to = params.get("to") || undefined;
  const obj = params.get("obj") || undefined;
  const decision = oneOf(params.get("decision"), ["none", "any", "dispatch_crew", "remote_check", "defer", "reject"] as const);
  const outcome = oneOf(params.get("outcome"), ["hit", "miss", "unknown"] as const);
  const groupBy = oneOf(params.get("group_by"), ["obj", "case_key"] as const);
  const unsupportedFiltersActive = Boolean(from || to || decision || outcome || obj || groupBy);
  const load = useLoad(() => api.GET("/api/v1/forecasts", { params: { query: { scenario, from, to, decision, outcome, obj, group_by: groupBy, page, page_size: PAGE_SIZE } } }), [scenario, from, to, decision, outcome, obj, groupBy, page]);

  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (value === undefined || value === "" || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }
  function reset() { setParams({}); }
  function toggle(item: Forecast) { setCreatedOrder(null); setCreateError(null); setSelected((items) => items.some((value) => value.id === item.id) ? items.filter((value) => value.id !== item.id) : [...items, item]); }
  function compatible(item: Forecast): boolean { const first = selected[0]; return !first || (first.scenario === item.scenario && objectKey(first) === objectKey(item)); }
  async function createOrder() {
    if (!selected.length) return; setCreating(true); setCreateError(null);
    try { const { data, error, response } = await api.POST("/api/v1/work-orders", { body: { forecast_ids: selected.map((item) => item.id) } }); if (data) { setCreatedOrder(data.id); setSelected([]); load.reload(); } else setCreateError(errorText(error, response)); }
    catch { setCreateError(errorText(null, undefined)); } finally { setCreating(false); }
  }
  return <section>
    <PageHeader eyebrow="Предиктивная аналитика" title="Журнал прогнозов" description="Единая очередь рисков с решениями диспетчера и результатами проверки" actions={<>{can("export") && <button className="button" type="button" disabled title="Экспорт пока содержит только заголовки — ожидается реализация сервера"><Icon name="download" /> Экспорт Excel — готовится</button>}<button className="button" type="button" onClick={load.reload}>Обновить</button></>} />
    <div className="filter-panel">
      <label className="field"><span>Сценарий</span><select value={scenario ?? ""} onChange={(e) => update("scenario", e.target.value)}><option value="">Все сценарии</option>{SCENARIOS.map((s) => <option key={s.code} value={s.code}>{s.title}</option>)}</select></label>
      <label className="field"><span>Дата от</span><input type="date" value={from ?? ""} onChange={(e) => update("from", e.target.value)} /></label>
      <label className="field"><span>Дата до</span><input type="date" value={to ?? ""} onChange={(e) => update("to", e.target.value)} /></label>
      <label className="field"><span>Решение</span><select value={decision ?? ""} onChange={(e) => update("decision", e.target.value)}><option value="">Все решения</option><option value="none">Без решения</option><option value="any">Решение принято</option>{ACTIONS.map((a) => <option key={a.code} value={a.code}>{a.title}</option>)}</select></label>
      <label className="field"><span>Факт</span><select value={outcome ?? ""} onChange={(e) => update("outcome", e.target.value)}><option value="">Любой</option><option value="hit">Попадание</option><option value="miss">Промах</option><option value="unknown">Неизвестно</option></select></label>
      <label className="field"><span>Объект</span><input key={obj ?? ""} defaultValue={obj ?? ""} onBlur={(e) => update("obj", e.target.value.trim())} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); update("obj", e.currentTarget.value.trim()); } }} placeholder="ID или название · Enter" /></label>
      <label className="field"><span>Группировка</span><select value={groupBy ?? ""} onChange={(e) => update("group_by", e.target.value)}><option value="">Без группировки</option><option value="obj">По объекту</option><option value="case_key">По случаю</option></select></label>
      <button type="button" className="button filter-reset" onClick={reset}><Icon name="filter" /> Сбросить</button>
    </div>
    {unsupportedFiltersActive && <p className="filter-notice" role="status">В текущем API работает только фильтр сценария. Дата, решение, факт, объект и группировка пока не применяются: журнал ниже показан без этих ограничений.</p>}
    <Loaded load={load}>{(data) => <>
      <div className="section-summary"><span className="summary-pill">Найдено <strong>{data.total}</strong></span><span className="summary-pill">Попаданий на странице <strong>{data.items.filter((i) => i.outcome_auto === "hit").length}</strong></span><span className="summary-pill">Неизвестно <strong>{data.items.filter((i) => i.outcome_auto === "unknown" || i.outcome_auto == null).length}</strong></span></div>
      {createdOrder && <div className="action-success"><Icon name="orders" /><span>Черновик <strong>{createdOrder}</strong> сформирован</span><Link to={`/work-orders?open=${encodeURIComponent(createdOrder)}`}>Открыть заявку</Link></div>}
      {createError && <div className="state state--bad">{createError}</div>}
      {can("work_order_manage") && selected.length > 0 && <div className="bulk-bar"><div><strong>Выбрано: {selected.length}</strong><span>{selected[0].object.name ?? selected[0].object.id} · {selected[0].scenario_title}</span></div><button className="button" type="button" onClick={() => setSelected([])}>Отменить</button><button className="button button--primary" type="button" disabled={creating} onClick={() => void createOrder()}>{creating ? "Формирование…" : "Создать общую заявку"}</button></div>}
      {data.items.length === 0 ? <StateView state="empty" /> : <ForecastTable items={data.items} selectable={can("work_order_manage")} selected={selected} compatible={compatible} onToggle={toggle} />}
      <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(value) => update("page", value)} />
    </>}</Loaded>
  </section>;
}

function ForecastTable({ items, selectable, selected, compatible, onToggle }: { items: Forecast[]; selectable: boolean; selected: Forecast[]; compatible: (item: Forecast) => boolean; onToggle: (item: Forecast) => void }) {
  return <div className="table-wrap"><table className="table forecast-table"><thead><tr>{selectable && <th className="select-cell"><span className="sr-only">Выбор</span></th>}<th>Приоритет</th><th>Прогноз и окно</th><th>Направление</th><th>Объект</th><th>Оценка</th><th>Решение</th><th>Факт по данным</th><th>Итог проверки</th><th /></tr></thead><tbody>{items.map((item) => { const checked = selected.some((value) => value.id === item.id); const disabled = Boolean(item.work_order_id) || (!checked && !compatible(item)); return <tr key={item.id} className={checked ? "forecast-row--selected" : ""}>
    {selectable && <td className="select-cell"><input type="checkbox" checked={checked} disabled={disabled} onChange={() => onToggle(item)} aria-label={disabled && item.work_order_id ? "Заявка уже сформирована" : `Выбрать прогноз ${item.id}`} title={disabled ? item.work_order_id ? "Уже включён в заявку" : "Для общей заявки выберите тот же объект и сценарий" : "Добавить в общую заявку"} /></td>}
    <td><span className={`rank-badge ${item.rank <= 3 ? "rank-badge--hot" : ""}`}>{item.rank}</span></td>
    <td><strong>{fmtDate(item.asof)}</strong><small className="cell-sub">{fmtDateTime(item.valid_from)} — {fmtDateTime(item.valid_to)}</small></td>
    <td>{item.scenario_title}<small className="cell-sub">Горизонт {item.horizon_hours} ч</small></td>
    <td><Link className="object-link" to={`/forecasts/${encodeURIComponent(item.id)}`}>{placeText(item)}</Link> <SourceBadge source={item.source} /></td>
    <td>{scoreText(item)}</td><td>{item.decision ? <span className="chip chip--info">{title("action", item.decision.action)}</span> : <span className="chip chip--muted">Не принято</span>}</td>
    <td><span className={`outcome outcome--${item.outcome_auto ?? "unknown"}`}>{title("outcome_auto", item.outcome_auto)}</span></td><td><span className={`outcome outcome--${item.outcome_manual === "confirmed_event" ? "hit" : item.outcome_manual === "no_event" ? "miss" : "unknown"}`}>{title("outcome_manual", item.outcome_manual)}</span></td><td><Link className="row-arrow" to={`/forecasts/${encodeURIComponent(item.id)}`}><Icon name="arrow" /></Link></td>
  </tr>; })}</tbody></table></div>;
}
