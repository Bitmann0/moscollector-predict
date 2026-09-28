import { useMemo, type FocusEvent, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtNumber, pageParam } from "../format";
import { useReloadOn } from "../stream/useStream";
import { usePersistentBoolean } from "../usePersistentState";
import { title } from "../vocab";

type EventItem = Schemas["EventItem"];
type TreeNode = Schemas["TreeNode"];
type EventClass = EventItem["event_class"];

const PAGE_SIZE = 100;
const CLASSES: EventClass[] = ["normal", "warning", "alarm", "critical", "fault", "service"];
// Списка типов датчиков в API нет. Перечень — все 19 типов из Materials/справочник_каналов_датчиков.csv;
// backend ищет тип подстрокой, поэтому значение из ссылки (например, «газ») тоже работает.
const SENSOR_TYPES = ["9-секционный люк", "ИБП", "Газовый датчик", "Датчик движения", "Датчик дыма", "Датчик затопления", "Датчик температуры", "КД АВ", "КД Дверь", "КД Люк", "Переключатель", "Ручной извещатель", "Состояние вентилятора", "Состояние насоса", "Состояние охраны", "Состояние УИР-Р", "Состояние фазы", "Стекло", "Тепловой датчик"].sort((a, b) => a.localeCompare(b, "ru"));
// Единицы — раздел C5 плана команды: газ в процентах объёма метана, температура в °C (порог сбоя −60…150).
const UNITS: Record<string, string> = { "Газовый датчик": "% об.", "Датчик температуры": "°C" };
const DECIMAL = /^-?\d+(?:\.(\d+))?$/;
const EPOCH = "01.01.1970 03:00:0";
const TIME = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

function eventClass(value: string | null): EventClass | undefined { return CLASSES.find((item) => item === value); }
function fmtTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? value : TIME.format(date); }
function plural(n: number, forms: [string, string, string]): string { const d = n % 10, h = n % 100; return forms[d === 1 && h !== 11 ? 0 : d >= 2 && d <= 4 && (h < 12 || h > 14) ? 1 : 2]; }

/** Значение датчика по-русски: «0.01» газа — «0,01 % об.», «26» температуры — «26 °C». */
function eventText(event: EventItem): string {
  const raw = event.sensor_event?.trim();
  if (!raw) return event.event_class_title;
  // Контроллер присылает «01.01.1970 03:00:0x» вместо значения — это сбой, а не время события (C5).
  if (raw.startsWith(EPOCH)) return "Дата вместо значения";
  const match = DECIMAL.exec(raw);
  if (!match) return raw;
  const digits = match[1]?.length ?? 0;
  const number = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(raw));
  const unit = UNITS[event.channel.sensor_type ?? ""];
  return unit ? `${number} ${unit}` : number;
}

/** Объекты справочника по комплексам: 78 объектов с каналами проще выбрать из списка, чем вспомнить название. */
function objectGroups(nodes: TreeNode[]): { label: string; options: TreeNode[] }[] {
  const groups: { label: string; options: TreeNode[] }[] = [];
  const walk = (node: TreeNode) => {
    const kids = node.children ?? [];
    const leaves = kids.filter((kid) => !kid.children?.length && kid.channels > 0).sort((a, b) => a.name.localeCompare(b.name, "ru"));
    if (leaves.length) groups.push({ label: node.name, options: leaves });
    kids.forEach(walk);
  };
  nodes.forEach(walk);
  return groups;
}

export function Events() {
  const [params, setParams] = useSearchParams();
  const page = pageParam(params.get("page"));
  const from = params.get("from") || undefined, to = params.get("to") || undefined, obj = params.get("obj") || undefined, sensorType = params.get("sensor_type") || undefined, q = params.get("q") || undefined;
  const cls = eventClass(params.get("event_class"));
  const [auto, setAuto] = usePersistentBoolean("mkl.events.auto-refresh", true);
  const [hideNormalGas, setHideNormalGas] = usePersistentBoolean("mkl.events.hide-normal-gas", true);
  // Выбранный тип датчика сам решает, нужен ли газ, а класс без нормы штатного газа не содержит:
  // в обоих случаях флажок не действует. Скрывает backend, поэтому total и страницы — по всему журналу.
  const hideApplies = !sensorType && (!cls || cls === "normal");
  const hide = hideNormalGas && hideApplies;
  const load = useLoad(() => api.GET("/api/v1/events", { params: { query: { from, to, obj, sensor_type: sensorType, event_class: cls, q, hide_normal_gas: hide || undefined, page, page_size: PAGE_SIZE } } }), [from, to, obj, sensorType, cls, q, hide, page]);
  const tree = useLoad(() => api.GET("/api/v1/reference/tree"), []);
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  const groups = useMemo(() => objectGroups(tree.data ?? []), [tree.data]);
  useReloadOn(["alert.new", "event.alarm"], load.reload, { enabled: auto });
  const filtered = Boolean(from || to || obj || sensorType || cls || q);
  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (!value || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }
  function applyText(key: string) { return { onBlur: (e: FocusEvent<HTMLInputElement>) => update(key, e.target.value.trim()), onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => { if (e.key === "Enter") { e.preventDefault(); update(key, e.currentTarget.value.trim()); } } }; }
  const knownObject = !obj || groups.some((group) => group.options.some((item) => item.id === obj));

  return <section>
    <PageHeader eyebrow="Мониторинг СМВУ" title="Журнал событий" description="Поток сигналов, тревог и сервисных сообщений инженерной инфраструктуры" actions={<label className={`live-toggle ${auto ? "live-toggle--on" : ""}`}><input type="checkbox" role="switch" checked={auto} onChange={(e) => setAuto(e.target.checked)} /><i />{auto ? "Автообновление" : "Обновление выключено"}</label>} />
    {/* Время событий — июнь 2026, а не сегодняшний день: без пояснения журнал выглядит остановившимся. */}
    <p className="events-note">Журнал СМВУ заказчика в воспроизведении: события демо-дня{status.data ? ` ${fmtDate(status.data.demo_today)}` : ""} идут по часам МСК, более ранние — загруженная история.</p>
    <div className="filter-panel events-filters">
      <label className="field"><span>Дата от</span><input type="date" value={from ?? ""} max={to} onChange={(e) => update("from", e.target.value)} /></label>
      <label className="field"><span>Дата до</span><input type="date" value={to ?? ""} min={from} onChange={(e) => update("to", e.target.value)} /></label>
      <label className="field"><span>Объект</span><select value={obj ?? ""} onChange={(e) => update("obj", e.target.value)}><option value="">Все объекты</option>{!knownObject && <option value={obj}>Объект {obj}</option>}{groups.map((group) => <optgroup key={group.label} label={group.label}>{group.options.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</optgroup>)}</select></label>
      <label className="field"><span>Тип датчика</span><select value={sensorType ?? ""} onChange={(e) => update("sensor_type", e.target.value)}><option value="">Все типы</option>{sensorType && !SENSOR_TYPES.includes(sensorType) && <option value={sensorType}>{sensorType}</option>}{SENSOR_TYPES.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
      <label className="field"><span>Класс события</span><select value={cls ?? ""} onChange={(e) => update("event_class", e.target.value)}><option value="">Все классы</option>{CLASSES.map((item) => <option key={item} value={item}>{title("event_class", item)}</option>)}</select></label>
      <label className="field"><span>Поиск</span><input key={q ?? ""} defaultValue={q ?? ""} {...applyText("q")} placeholder="Текст события" title="Enter — применить" /></label>
    </div>
    <div className="events-toolbar">
      <label className={`check quiet-check${hideApplies ? "" : " quiet-check--off"}`}><input type="checkbox" checked={hideNormalGas} disabled={!hideApplies} onChange={(e) => { setHideNormalGas(e.target.checked); update("page", 1); }} /><span>Скрывать штатные показания газовых датчиков{!hideApplies && <small>{sensorType ? "не действует при выбранном типе датчика" : "выбранный класс не включает норму"}</small>}</span></label>
      {load.data && <span className="events-summary">{found(load.data.total)}</span>}
      {filtered && <button type="button" className="button events-reset" onClick={() => setParams(new URLSearchParams())}>Сбросить фильтры</button>}
    </div>
    <Loaded load={load}>{(data) => <>
      {data.items.length === 0 ? <StateView state="empty" detail={emptyText(page, filtered, hide)} /> : <div className="table-wrap events-wrap"><table className="table events-table">
        <thead><tr><th>Время регистрации</th><th>Объект</th><th>Тип датчика</th><th>Событие датчика</th><th>Тип события</th></tr></thead>
        <tbody>{data.items.map((event) => <tr key={event.id} className={`events-row events-row--${event.event_class}`}>
          <td className="events-col-time"><time dateTime={event.ts}>{fmtTime(event.ts)}</time></td>
          <td className="events-col-object"><strong>{event.object.name ?? event.object.id ?? "Объект не указан"}</strong>{event.channel.name && <small>{event.channel.name}</small>}</td>
          <td className="events-col-type">{event.channel.sensor_type ?? "Тип не указан"}</td>
          <td className="events-col-event">{eventText(event)}{event.sensor_event?.startsWith(EPOCH) && <small className="events-raw">{event.sensor_event}</small>}{event.hint && <small className="events-hint">{event.hint}</small>}</td>
          <td className="events-col-class"><span className={`event-status event-status--${event.event_class}`}>{event.event_class_title}</span></td>
        </tr>)}</tbody>
      </table></div>}
      <Pager page={data.page} pageSize={data.page_size} total={data.total} onPage={(value) => update("page", value)} />
    </>}</Loaded>
  </section>;
}

function found(total: number): string { return `Найдено ${fmtNumber(total)} ${plural(total, ["событие", "события", "событий"])}`; }

function emptyText(page: number, filtered: boolean, hide: boolean): string {
  if (page > 1) return `На странице ${page} событий нет — вернитесь к первой.`;
  const gas = hide ? " Штатные показания газа скрыты — снимите флажок, чтобы увидеть и их." : "";
  return (filtered ? "По выбранным фильтрам событий нет." : "В журнале нет событий.") + gas;
}
