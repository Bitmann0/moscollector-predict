import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime, pageParam } from "../format";
import { useReloadOn } from "../stream/useStream";

const PAGE_SIZE = 100;
type EventClass = Schemas["EventItem"]["event_class"];
const CLASSES: EventClass[] = ["normal", "warning", "alarm", "critical", "fault", "service"];
function eventClass(value: string | null): EventClass | undefined { return CLASSES.find((item) => item === value); }

export function Events() {
  const [params, setParams] = useSearchParams();
  const page = pageParam(params.get("page"));
  const from = params.get("from") || undefined, to = params.get("to") || undefined, obj = params.get("obj") || undefined, sensorType = params.get("sensor_type") || undefined, q = params.get("q") || undefined;
  const cls = eventClass(params.get("event_class"));
  const [auto, setAuto] = useState(true);
  const [hideNormalGas, setHideNormalGas] = useState(true);
  const load = useLoad(() => api.GET("/api/v1/events", { params: { query: { from, to, obj, sensor_type: sensorType, event_class: cls, q, page, page_size: PAGE_SIZE } } }), [from, to, obj, sensorType, cls, q, page]);
  useReloadOn(["alert.new", "event.alarm"], load.reload, { enabled: auto });
  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (!value || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }

  return <section>
    <PageHeader eyebrow="Мониторинг СМВУ" title="Журнал событий" description="Поток сигналов, тревог и сервисных сообщений инженерной инфраструктуры" actions={<label className={`live-toggle ${auto ? "live-toggle--on" : ""}`}><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /><i />{auto ? "Автообновление" : "Обновление выключено"}</label>} />
    <div className="filter-panel events-filters">
      <label className="field"><span>Дата от</span><input type="date" value={from ?? ""} onChange={(e) => update("from", e.target.value)} /></label><label className="field"><span>Дата до</span><input type="date" value={to ?? ""} onChange={(e) => update("to", e.target.value)} /></label>
      <label className="field"><span>Объект</span><input value={obj ?? ""} onChange={(e) => update("obj", e.target.value)} placeholder="Название или ID" /></label><label className="field"><span>Тип датчика</span><input value={sensorType ?? ""} onChange={(e) => update("sensor_type", e.target.value)} placeholder="Дым, газ, температура…" /></label>
      <label className="field"><span>Класс события</span><select value={cls ?? ""} onChange={(e) => update("event_class", e.target.value)}><option value="">Все классы</option>{CLASSES.map((item) => <option key={item} value={item}>{eventTitle(item)}</option>)}</select></label><label className="field"><span>Поиск</span><input value={q ?? ""} onChange={(e) => update("q", e.target.value)} placeholder="Текст события" /></label>
    </div>
    <label className="check quiet-check"><input type="checkbox" checked={hideNormalGas} onChange={(e) => setHideNormalGas(e.target.checked)} />Скрывать штатные показания газовых датчиков</label>
    <Loaded load={load}>{(data) => { const items = data.items.filter((event) => !(hideNormalGas && event.event_class === "normal" && /газ/i.test(event.channel.sensor_type ?? ""))); return <>{items.length === 0 ? <StateView state="empty" /> : <div className="event-feed">{items.map((event) => <article key={event.id} className={`event-card event-card--${event.event_class}`}><div className="event-card__marker" /><time>{fmtDateTime(event.ts)}</time><div className="event-card__body"><strong>{event.sensor_event ?? event.event_class_title}</strong><span>{event.object.name ?? event.object.id ?? "Объект не указан"} · {event.channel.sensor_type ?? "Тип датчика не указан"}</span>{event.hint && <em>{event.hint}</em>}</div><span className={`event-status event-status--${event.event_class}`}>{event.event_class_title}</span></article>)}</div>}<Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(value) => update("page", value)} /></>; }}</Loaded>
  </section>;
}

function eventTitle(value: EventClass): string { return ({ normal: "Норма", warning: "Предупреждение", alarm: "Тревога", critical: "Критическое", fault: "Неисправность", service: "Сервисное" })[value]; }
