import { useSearchParams } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime, pageParam } from "../format";
import { useReloadOn } from "../stream/useStream";
import { usePersistentBoolean } from "../usePersistentState";
import { title } from "../vocab";

const PAGE_SIZE = 100;
type EventClass = Schemas["EventItem"]["event_class"];
const CLASSES: EventClass[] = ["normal", "warning", "alarm", "critical", "fault", "service"];
function eventClass(value: string | null): EventClass | undefined { return CLASSES.find((item) => item === value); }

export function Events() {
  const [params, setParams] = useSearchParams();
  const page = pageParam(params.get("page"));
  const from = params.get("from") || undefined, to = params.get("to") || undefined, obj = params.get("obj") || undefined, sensorType = params.get("sensor_type") || undefined, q = params.get("q") || undefined;
  const cls = eventClass(params.get("event_class"));
  const unsupportedFiltersActive = Boolean(from || to || obj || sensorType || cls || q);
  const [auto, setAuto] = usePersistentBoolean("mkl.events.auto-refresh", true);
  const [hideNormalGas, setHideNormalGas] = usePersistentBoolean("mkl.events.hide-normal-gas", true);
  const load = useLoad(() => api.GET("/api/v1/events", { params: { query: { from, to, obj, sensor_type: sensorType, event_class: cls, q, page: hideNormalGas ? 1 : page, page_size: hideNormalGas ? 1000 : PAGE_SIZE } } }), [from, to, obj, sensorType, cls, q, page, hideNormalGas]);
  useReloadOn(["alert.new", "event.alarm"], load.reload, { enabled: auto });
  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (!value || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }

  return <section>
    <PageHeader eyebrow="Мониторинг СМВУ" title="Журнал событий" description="Поток сигналов, тревог и сервисных сообщений инженерной инфраструктуры" actions={<label className={`live-toggle ${auto ? "live-toggle--on" : ""}`}><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /><i />{auto ? "Автообновление" : "Обновление выключено"}</label>} />
    <p className="filter-notice">Демонстрационный поток: сервер пока формирует синтетические события. Не используйте этот журнал для подтверждения реальных тревог.</p>
    <div className="filter-panel events-filters">
      <label className="field"><span>Дата от</span><input type="date" value={from ?? ""} onChange={(e) => update("from", e.target.value)} /></label><label className="field"><span>Дата до</span><input type="date" value={to ?? ""} onChange={(e) => update("to", e.target.value)} /></label>
      <label className="field"><span>Объект</span><input key={obj ?? ""} defaultValue={obj ?? ""} onBlur={(e) => update("obj", e.target.value.trim())} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); update("obj", e.currentTarget.value.trim()); } }} placeholder="Название или ID" title="Enter — применить" /></label><label className="field"><span>Тип датчика</span><input key={sensorType ?? ""} defaultValue={sensorType ?? ""} onBlur={(e) => update("sensor_type", e.target.value.trim())} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); update("sensor_type", e.currentTarget.value.trim()); } }} placeholder="Дым, газ, температура…" /></label>
      <label className="field"><span>Класс события</span><select value={cls ?? ""} onChange={(e) => update("event_class", e.target.value)}><option value="">Все классы</option>{CLASSES.map((item) => <option key={item} value={item}>{eventTitle(item)}</option>)}</select></label><label className="field"><span>Поиск</span><input key={q ?? ""} defaultValue={q ?? ""} onBlur={(e) => update("q", e.target.value.trim())} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); update("q", e.currentTarget.value.trim()); } }} placeholder="Текст события" title="Enter — применить" /></label>
    </div>
    <label className="check quiet-check"><input type="checkbox" checked={hideNormalGas} onChange={(e) => { setHideNormalGas(e.target.checked); update("page", 1); }} />Скрывать штатные показания газовых датчиков</label>
    {unsupportedFiltersActive && <p className="filter-notice" role="status">В текущем API дата, объект, тип датчика, класс события и поиск пока не применяются. Ниже показан общий поток; переключатель штатных показаний действует на загруженные события.</p>}
    <Loaded load={load}>{(data) => { const filtered = data.items.filter((event) => !(hideNormalGas && event.event_class === "normal" && /газ/i.test(event.channel.sensor_type ?? ""))); const items = hideNormalGas ? filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE) : filtered; const total = hideNormalGas ? filtered.length : data.total; return <>{hideNormalGas && data.total > data.items.length && <p className="filter-notice">Шум отфильтрован среди последних {data.items.length} событий. Более ранние записи в эту выборку не вошли.</p>}{items.length === 0 ? <StateView state="empty" detail="После подавления штатного шума в загруженной выборке событий нет." /> : <div className="event-feed">{items.map((event) => <article key={event.id} className={`event-card event-card--${event.event_class}`}><div className="event-card__marker" /><time>{fmtDateTime(event.ts)}</time><div className="event-card__body"><strong>{event.sensor_event ?? event.event_class_title}</strong><span>{event.object.name ?? event.object.id ?? "Объект не указан"} · {event.channel.sensor_type ?? "Тип датчика не указан"}</span>{event.hint && <em>{event.hint}</em>}</div><span className={`event-status event-status--${event.event_class}`}>{event.event_class_title}</span></article>)}</div>}<Pager page={page} pageSize={PAGE_SIZE} total={total} onPage={(value) => update("page", value)} /></>; }}</Loaded>
  </section>;
}

function eventTitle(value: EventClass): string { return title("event_class", value); }
