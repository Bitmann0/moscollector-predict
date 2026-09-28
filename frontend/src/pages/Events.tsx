import { useCallback, useEffect, useMemo, useRef, useState, type FocusEvent, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad, type Load, type LoadState } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Pager } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtNumber, pageParam } from "../format";
import { useReloadOn } from "../stream/useStream";
import { usePersistentBoolean } from "../usePersistentState";
import { title } from "../vocab";

type EventItem = Schemas["EventItem"];
type TreeNode = Schemas["TreeNode"];
type EventClass = EventItem["event_class"];
type Query = { from?: string; to?: string; obj?: string; sensor_type?: string; event_class?: EventClass; q?: string };
interface Scan { scanned: number; hidden: number; oldest?: string; complete: boolean; need: number; partial?: boolean }
interface Journal { items: EventItem[]; total: number; scan?: Scan }

const PAGE_SIZE = 100;
const CLASSES: EventClass[] = ["normal", "warning", "alarm", "critical", "fault", "service"];
// API не умеет исключать класс, поэтому штатный газ скрывается здесь: журнал читается
// пачками по 1000 с конца. Одной пачки мало: на стенде 1000 записей ночью — около 9 минут
// потока, из них вне штатного газа 8. Десять пачек (до 22:31 29.06) дают 211 событий,
// в том числе первые тревоги; дальше — фильтр по дате. Пачки идут по одной: параллельные
// запросы backend выполняет не быстрее (замер: 2,9 с на две сразу, 3,5 с на четыре).
const CHUNK = 1000;
const MAX_CHUNKS = 10;
// Backend сравнивает тип датчика точно, а списка типов в API нет: свободный ввод «газ»
// находил ноль событий после 18 с полного прохода. Перечень — ref_channels стенда на 30.06.2026.
const SENSOR_TYPES = ["9-секционный люк", "ИБП", "Газовый датчик", "Датчик движения", "Датчик дыма", "Датчик затопления", "Датчик температуры", "КД АВ", "КД Дверь", "КД Люк", "Переключатель", "Ручной извещатель", "Состояние вентилятора", "Состояние насоса", "Состояние охраны", "Состояние УИР-Р", "Состояние фазы", "Стекло", "Тепловой датчик"].sort((a, b) => a.localeCompare(b, "ru"));
// Единицы — раздел C5 плана команды: газ в процентах объёма метана.
const UNITS: Record<string, string> = { "Газовый датчик": "% об.", "Датчик температуры": "°C" };
const DECIMAL = /^-?\d+(?:\.(\d+))?$/;
const EPOCH = "01.01.1970 03:00:0";
const TIME = new Intl.DateTimeFormat("ru-RU", { timeZone: "Europe/Moscow", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });

function eventClass(value: string | null): EventClass | undefined { return CLASSES.find((item) => item === value); }
function isNormalGas(event: EventItem): boolean { return event.event_class === "normal" && /газ/i.test(event.channel.sensor_type ?? ""); }
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
  return unit ? `${number} ${unit}` : number;
}

/** Объекты справочника по комплексам: фильтр API принимает id объекта, а не название. */
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

type Result = { data?: Journal; error?: unknown; response: Response };

/**
 * Серверная страница или просмотр журнала с конца без штатного газа. onPartial получает
 * уже найденное после каждой пачки: первые строки видны через одну пачку, а не через все.
 * alive() ложно, когда фильтр сменился, — тогда просмотр бросается, чтобы не грузить backend.
 */
async function loadJournal(query: Query, scan: boolean, page: number, alive: () => boolean, onPartial?: (data: Journal) => void): Promise<Result> {
  if (!scan) {
    const result = await api.GET("/api/v1/events", { params: { query: { ...query, page, page_size: PAGE_SIZE } } });
    return { data: result.data && { items: result.data.items, total: result.data.total }, error: result.error, response: result.response };
  }
  // Страница N требует N·100 событий и ещё одно — чтобы знать, есть ли следующая.
  const need = page * PAGE_SIZE + 1;
  const items: EventItem[] = [];
  let scanned = 0, hidden = 0, total = Infinity, oldest: string | undefined, response: Response | undefined;
  const journal = (partial: boolean): Journal => ({ items: [...items], total: items.length, scan: { scanned, hidden, oldest, complete: scanned >= total, need, partial } });
  for (let chunk = 1; chunk <= MAX_CHUNKS && items.length < need && scanned < total && alive(); chunk += 1) {
    const result = await api.GET("/api/v1/events", { params: { query: { ...query, page: chunk, page_size: CHUNK } } });
    response = result.response;
    if (!result.data) return { error: result.error, response: result.response };
    total = result.data.total;
    scanned += result.data.items.length;
    for (const event of result.data.items) if (isNormalGas(event)) hidden += 1; else items.push(event);
    oldest = result.data.items[result.data.items.length - 1]?.ts ?? oldest;
    // Пустую промежуточную выборку не показываем: «Записей нет» до конца просмотра было бы неправдой.
    if (onPartial && items.length > 0 && items.length < need && scanned < total && chunk < MAX_CHUNKS) onPartial(journal(true));
  }
  return { data: journal(false), response: response as Response };
}

/**
 * useLoad с промежуточными результатами просмотра. При перезапросе по потоку промежуточные
 * данные не показываются: иначе таблица на секунду укоротилась бы до одной пачки.
 */
function useJournal(query: Query, scan: boolean, page: number): Load<Journal> {
  const [state, setState] = useState<LoadState<Journal>>({ status: "loading" });
  const [tick, setTick] = useState(0);
  const seq = useRef(0);
  const lastTick = useRef(tick);
  const key = JSON.stringify([query, scan, page]);
  useEffect(() => {
    const id = ++seq.current;
    const alive = () => id === seq.current;
    const isReload = tick !== lastTick.current;
    lastTick.current = tick;
    setState((prev) => ({ status: "loading", data: isReload ? prev.data : undefined }));
    loadJournal(query, scan, page, alive, isReload ? undefined : (data) => { if (alive()) setState({ status: "ok", data }); })
      .then(({ data, error, response }) => {
        if (!alive()) return;
        if (data !== undefined && response.ok) setState({ status: "ok", data });
        else setState((prev) => ({ status: "error", message: errorText(error, response), data: prev.data }));
      })
      .catch(() => { if (alive()) setState((prev) => ({ status: "error", message: errorText(null, undefined), data: prev.data })); });
    // query пересобирается на каждом рендере; запрос меняется только вместе с key.
  }, [key, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { ...state, reload };
}

export function Events() {
  const [params, setParams] = useSearchParams();
  const page = pageParam(params.get("page"));
  const from = params.get("from") || undefined, to = params.get("to") || undefined, obj = params.get("obj") || undefined, sensorType = params.get("sensor_type") || undefined, q = params.get("q") || undefined;
  const cls = eventClass(params.get("event_class"));
  const [auto, setAuto] = usePersistentBoolean("mkl.events.auto-refresh", true);
  const [hideNormalGas, setHideNormalGas] = usePersistentBoolean("mkl.events.hide-normal-gas", true);
  // Выбранный тип датчика или класс без нормы сам отсекает штатный газ (или явно просит его):
  // тогда флажок не действует, и журнал листается сервером с точным числом записей.
  const hideApplies = !sensorType && (!cls || cls === "normal");
  const scan = hideNormalGas && hideApplies;
  const load = useJournal({ from, to, obj, sensor_type: sensorType, event_class: cls, q }, scan, page);
  const tree = useLoad(() => api.GET("/api/v1/reference/tree"), []);
  const groups = useMemo(() => objectGroups(tree.data ?? []), [tree.data]);
  useReloadOn(["alert.new", "event.alarm"], load.reload, { enabled: auto });
  const filtered = Boolean(from || to || obj || sensorType || cls || q);
  function update(key: string, value?: string | number) { const next = new URLSearchParams(params); if (!value || (key === "page" && value === 1)) next.delete(key); else next.set(key, String(value)); if (key !== "page") next.delete("page"); setParams(next); }
  function applyText(key: string) { return { onBlur: (e: FocusEvent<HTMLInputElement>) => update(key, e.target.value.trim()), onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => { if (e.key === "Enter") { e.preventDefault(); update(key, e.currentTarget.value.trim()); } } }; }
  const knownObject = !obj || groups.some((group) => group.options.some((item) => item.id === obj));

  return <section>
    <PageHeader eyebrow="Мониторинг СМВУ" title="Журнал событий" description="Поток сигналов, тревог и сервисных сообщений инженерной инфраструктуры" actions={<label className={`live-toggle ${auto ? "live-toggle--on" : ""}`}><input type="checkbox" role="switch" checked={auto} onChange={(e) => setAuto(e.target.checked)} /><i />{auto ? "Автообновление" : "Обновление выключено"}</label>} />
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
      {load.data && <span className="events-summary">{summary(load.data)}</span>}
      {filtered && <button type="button" className="button events-reset" onClick={() => setParams(new URLSearchParams())}>Сбросить фильтры</button>}
    </div>
    <Loaded load={load}>{(data) => {
      const items = data.scan ? data.items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE) : data.items;
      const limited = data.scan && !data.scan.partial && !data.scan.complete && data.items.length < data.scan.need;
      return <>
        {items.length === 0 ? <StateView state="empty" detail={data.scan?.scanned ? `Среди ${fmtNumber(data.scan.scanned)} последних записей журнала нет событий, кроме штатных показаний газа. Выберите день в поле «Дата до» или снимите флажок.` : "По выбранным фильтрам событий нет."} /> : <div className="table-wrap events-wrap"><table className="table events-table">
          <thead><tr><th>Время регистрации</th><th>Объект</th><th>Тип датчика</th><th>Событие датчика</th><th>Тип события</th></tr></thead>
          <tbody>{items.map((event) => <tr key={event.id} className={`events-row events-row--${event.event_class}`}>
            <td className="events-col-time"><time dateTime={event.ts}>{fmtTime(event.ts)}</time></td>
            <td className="events-col-object"><strong>{event.object.name ?? event.object.id ?? "Объект не указан"}</strong>{event.channel.name && <small>{event.channel.name}</small>}</td>
            <td className="events-col-type">{event.channel.sensor_type ?? "Тип не указан"}</td>
            <td className="events-col-event">{eventText(event)}{event.sensor_event?.startsWith(EPOCH) && <small className="events-raw">{event.sensor_event}</small>}{event.hint && <small className="events-hint">{event.hint}</small>}</td>
            <td className="events-col-class"><span className={`event-status event-status--${event.event_class}`}>{event.event_class_title}</span></td>
          </tr>)}</tbody>
        </table></div>}
        {limited && items.length > 0 && <p className="events-more">Более ранние записи не просмотрены: выберите день в поле «Дата до» или снимите флажок.</p>}
        {data.scan ? (page > 1 || data.items.length > page * PAGE_SIZE) && <div className="pager"><button type="button" className="button" disabled={page <= 1} onClick={() => update("page", page - 1)}>Назад</button><span>{fmtNumber((page - 1) * PAGE_SIZE + Math.min(1, items.length))}–{fmtNumber((page - 1) * PAGE_SIZE + items.length)} · страница {page}</span><button type="button" className="button" disabled={data.items.length <= page * PAGE_SIZE} onClick={() => update("page", page + 1)}>Вперёд</button></div>
          : (page > 1 || data.total > PAGE_SIZE) && <Pager page={page} pageSize={PAGE_SIZE} total={data.total} onPage={(value) => update("page", value)} />}
      </>;
    }}</Loaded>
  </section>;
}

/** Строка рядом с флажком: сколько событий показано и сколько штатного газа скрыто из какого окна журнала. */
function summary(data: Journal): string {
  const found = (n: number) => `Найдено ${fmtNumber(n)} ${plural(n, ["событие", "события", "событий"])}`;
  if (!data.scan?.scanned) return found(data.total);
  const { scanned, hidden, oldest, complete, partial } = data.scan;
  if (complete) return hidden ? `${found(data.items.length)}, скрыто ${fmtNumber(hidden)} ${plural(hidden, ["штатное показание", "штатных показания", "штатных показаний"])} газа` : found(data.items.length);
  return `Скрыто ${fmtNumber(hidden)} из ${fmtNumber(scanned)} последних ${plural(scanned, ["записи", "записей", "записей"])}${oldest ? `, с ${fmtTime(oldest)}` : ""}${partial ? " — просматриваю дальше…" : ""}`;
}
