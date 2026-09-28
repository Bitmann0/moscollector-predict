import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { SourceBadge } from "../components/common";
import { PageHeader } from "../components/PageHeader";
import { Loaded, StateView } from "../components/StateView";
import { fmtNumber, fmtPercent } from "../format";

type Collection = Schemas["FeatureCollection"];
type Feature = Collection["features"][number];
type Forecast = Schemas["ForecastItem"];
type Point = [number, number];

async function loadOpenForecasts(): Promise<{ data?: Forecast[]; error?: unknown; response: Response }> {
  const first = await api.GET("/api/v1/forecasts", { params: { query: { decision: "none", page: 1, page_size: 500 } } });
  if (!first.data || !first.response.ok) return { error: first.error, response: first.response };
  const pages = Math.ceil(first.data.total / 500);
  if (pages <= 1) return { data: first.data.items.filter((item) => !item.decision), response: first.response };
  const items = [...first.data.items];
  for (let start = 2; start <= pages; start += 4) {
    const batch = await Promise.all(Array.from({ length: Math.min(4, pages - start + 1) }, (_, index) => api.GET("/api/v1/forecasts", { params: { query: { decision: "none", page: start + index, page_size: 500 } } })));
    const failed = batch.find((result) => !result.data || !result.response.ok);
    if (failed) return { error: failed.error, response: failed.response };
    items.push(...batch.flatMap((result) => result.data?.items ?? []));
  }
  return { data: items.filter((item) => !item.decision), response: first.response };
}

function coordinates(feature: Feature): Point[] {
  const raw = feature.geometry.coordinates;
  if (!Array.isArray(raw)) return [];
  if (typeof raw[0] === "number" && typeof raw[1] === "number") return [[raw[0], raw[1]]];
  return raw.flat(2).reduce<Point[]>((all, value, index, flat) => { if (index % 2 === 0 && typeof value === "number" && typeof flat[index + 1] === "number") all.push([value, flat[index + 1] as number]); return all; }, []);
}
function text(value: unknown, fallback = "—"): string { return typeof value === "string" || typeof value === "number" ? String(value) : fallback; }
function num(value: unknown): number | null { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function plural(count: number, one: string, few: string, many: string): string {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}
function counted(count: number, one: string, few: string, many: string): string { return `${fmtNumber(count)} ${plural(count, one, few, many)}`; }
const isLine = (feature: Feature) => String(feature.geometry.type).includes("Line");
const isObject = (feature: Feature) => String(feature.geometry.type).includes("Point") && feature.properties.feature !== "channel";

// Вид объекта в справочнике приходит кодом; подписи те же, что у ML (ml/src/mkl/address.py → obj_kind_ru).
const KIND_RU: Record<string, string> = { controlHouse: "диспетчерский пункт", guardObject: "охранная зона" };

// Геометрия — в пикселях экрана, а не в единицах viewBox: при масштабировании viewBox
// подписи на планшете падали до 8 px. Масштаб по пикетам меняет только ширину схемы.
const AXIS_H = 30;       // полоса подписей пикетов над строками
const ROW_MIN = 34;      // строка комплекса без стопок точек
const LANE = 12;         // шаг дорожки: точки одного комплекса ближе GAP расходятся вверх и вниз
const GAP = 14;          // минимум между центрами точек на одной дорожке, чтобы они не слипались
const BAND = 22;         // полоса подписи над линией на узком экране, где нет колонки подписей
const PAD_L = 20, PAD_R = 34;
const NARROW = 560;      // уже этого колонка подписей съедает половину схемы — подписи уходят в строку
const ZOOMS = [1, 1.5, 2, 3, 4, 6, 8];

interface MapObject { key: string; feature: Feature; id: string; name: string; x: number; count: number | null; risky: boolean; complexKey: string }
interface MapComplex { key: string; feature: Feature | null; id: string; name: string; x0: number; x1: number; y: number; count: number | null; risky: boolean; members: MapObject[] }
interface PlacedObject extends MapObject { px: number; py: number }
interface Row { complex: MapComplex; top: number; height: number; lineY: number; x0: number; x1: number; dots: PlacedObject[] }

export function Schema() {
  const load = useLoad(() => api.GET("/api/v1/schema.geojson"), []);
  const forecasts = useLoad(loadOpenForecasts, []);
  const riskState = forecasts.data ? "ok" : forecasts.status;
  return <section><PageHeader eyebrow="Топология" title="Схема сети" description="Интерактивная карта комплексов, сооружений и открытых рисков" /><div className="map-note">Условная схема. Координаты заказчиком не предоставлены.</div><Loaded load={load}>{(collection) => <NetworkMap collection={collection} forecasts={forecasts.data ?? []} riskState={riskState} onRiskRetry={forecasts.reload} />}</Loaded></section>;
}

function NetworkMap({ collection, forecasts: undecided, riskState, onRiskRetry }: { collection: Collection; forecasts: Forecast[]; riskState: "ok" | "loading" | "error"; onRiskRetry: () => void }) {
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  const canvasRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const pendingCenter = useRef<number | null>(null);

  // Счётчики схемы считает backend по правилу дашборда: без решения и окно не кончилось
  // к полуночи demo_today. Журнал с decision=none отдаёт и истёкшие прогнозы — без этого
  // фильтра паспорт показывал «0 открытых» и тут же список из шести прогнозов.
  const demoToday = typeof collection.properties.demo_today === "string" ? collection.properties.demo_today : null;
  const forecasts = useMemo(() => { if (!demoToday) return undecided; const midnight = Date.parse(`${demoToday}T00:00:00+03:00`); return Number.isNaN(midnight) ? undecided : undecided.filter((item) => Date.parse(item.valid_to) >= midnight); }, [undecided, demoToday]);

  const model = useMemo(() => {
    const byObject = new Map<string, number>(), byComplex = new Map<string, number>();
    for (const item of forecasts) { const id = item.object.id, complex = item.object.complex_id; if (id) byObject.set(id, (byObject.get(id) ?? 0) + 1); if (complex) byComplex.set(complex, (byComplex.get(complex) ?? 0) + 1); }
    const riskCount = (feature: Feature): number | null => { const fromSchema = num(feature.properties.open_forecasts); if (fromSchema !== null) return fromSchema; const id = text(feature.properties.id, ""); const joined = isLine(feature) ? (byComplex.get(id) ?? byComplex.get(text(feature.properties.complex_number, ""))) : byObject.get(id); return joined ?? (riskState === "ok" ? 0 : null); };
    const risky = (feature: Feature, count: number | null) => (count !== null && count > 0) || ["high", "critical"].includes(text(feature.properties.risk_level, ""));
    const all = collection.features.flatMap(coordinates);
    const xs = all.map(([x]) => x);
    const xMin = xs.length ? Math.min(...xs) : 0, xMax = xs.length ? Math.max(...xs) : 1;
    const complexes: MapComplex[] = collection.features.filter(isLine).map((feature, index) => {
      const pts = coordinates(feature), id = text(feature.properties.id, ""), count = riskCount(feature);
      const lx = pts.length ? pts.map(([x]) => x) : [xMin];
      return { key: `c:${id || index}`, feature, id, name: text(feature.properties.name, id || "Комплекс"), x0: Math.min(...lx), x1: Math.max(...lx), y: pts[0]?.[1] ?? 0, count, risky: risky(feature, count), members: [] };
    }).sort((a, b) => b.y - a.y);
    const byId = new Map(complexes.map((complex) => [complex.id, complex]));
    let orphans: MapComplex | null = null;
    collection.features.forEach((feature, index) => {
      if (!isObject(feature)) return;
      const [point] = coordinates(feature);
      if (!point) return;
      const id = text(feature.properties.id, ""), count = riskCount(feature);
      // Строка объекта — его комплекс; без complex_id — ближайшая строка сверху:
      // backend кладёт дорожки объектов ниже линии их комплекса.
      const above = complexes.filter((complex) => complex.y >= point[1]);
      let owner = byId.get(text(feature.properties.complex_id, "")) ?? (above.length ? above[above.length - 1] : undefined);
      if (!owner) { orphans ??= { key: "c:none", feature: null, id: "", name: "Вне комплексов", x0: point[0], x1: point[0], y: -Infinity, count: null, risky: false, members: [] }; owner = orphans; }
      owner.members.push({ key: `o:${id || index}`, feature, id, name: text(feature.properties.name, id || "Объект"), x: point[0], count, risky: risky(feature, count), complexKey: owner.key });
    });
    if (orphans) complexes.push(orphans);
    return { complexes, xMin, xMax, objects: complexes.flatMap((complex) => complex.members) };
  }, [collection, forecasts, riskState]);

  const layout = useMemo(() => {
    const narrow = width > 0 && width < NARROW;
    const longest = Math.max(0, ...model.complexes.map((complex) => complex.name.length + (complex.count ? 4 : 0)));
    const labelW = narrow ? 0 : Math.round(Math.min(220, Math.max(120, longest * 6.8 + 28)));
    const plotW = Math.max(280, Math.round(((width || 800) - labelW) * zoom));
    const scale = (plotW - PAD_L - PAD_R) / Math.max(1, model.xMax - model.xMin);
    const sx = (x: number) => PAD_L + (x - model.xMin) * scale;
    let top = AXIS_H;
    const rows: Row[] = model.complexes.map((complex) => {
      // Раскладка без наложений: точки с открытым прогнозом первыми занимают линию,
      // остальные — ближайшую свободную дорожку (0, −1, +1, −2, …) у своего пикета.
      const lanes = new Map<number, number[]>();
      const order = [...complex.members].sort((a, b) => Number(b.risky) - Number(a.risky) || a.x - b.x);
      const placed = order.map((item) => {
        const px = sx(item.x);
        let lane = 0;
        for (let step = 0; step < 60; step += 1) { const candidate = step % 2 ? -(step + 1) / 2 : step / 2; if ((lanes.get(candidate) ?? []).every((other) => Math.abs(other - px) >= GAP)) { lane = candidate; break; } }
        lanes.set(lane, [...(lanes.get(lane) ?? []), px]);
        return { item, px, lane };
      });
      const up = Math.max(0, ...placed.map((p) => -p.lane)), down = Math.max(0, ...placed.map((p) => p.lane));
      const band = narrow ? BAND : 0, content = (up + down) * LANE, inner = Math.max(ROW_MIN, content + 22);
      const lineY = top + band + (inner - content) / 2 + up * LANE;
      const row: Row = { complex, top, height: band + inner, lineY, x0: sx(complex.x0), x1: sx(complex.x1), dots: placed.map((p) => ({ ...p.item, px: p.px, py: lineY + p.lane * LANE })) };
      top += band + inner;
      return row;
    });
    const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000].find((value) => value * scale >= 80) ?? 10000;
    const ticks: { value: number; px: number }[] = [];
    for (let value = Math.ceil(model.xMin / step) * step; value <= model.xMax; value += step) ticks.push({ value, px: sx(value) });
    return { narrow, labelW, plotW, height: top + 8, rows, ticks };
  }, [model, width, zoom]);

  useLayoutEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const update = () => setWidth(el.clientWidth);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  // Паспорт липнет под шапкой, а её высота не постоянна: на ноутбуке 1280 строка шапки
  // переносится и она вырастает с 54 до 95 px — с постоянным top верх паспорта уходил под неё.
  useLayoutEffect(() => {
    const bar = document.querySelector<HTMLElement>(".statusbar"), panel = panelRef.current;
    if (!bar || !panel) return;
    const update = () => panel.style.setProperty("--map-sticky-top", `${Math.round(bar.getBoundingClientRect().height) + 14}px`);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(bar);
    return () => observer.disconnect();
  }, []);
  // После смены масштаба возвращаем в центр тот же пикет, что был в центре до неё.
  useLayoutEffect(() => {
    const el = canvasRef.current;
    if (!el || pendingCenter.current === null) return;
    el.scrollLeft = pendingCenter.current * layout.plotW - (el.clientWidth - layout.labelW) / 2;
    pendingCenter.current = null;
  }, [layout.plotW, layout.labelW]);

  const q = query.trim().toLowerCase();
  const hit = (name: string, id: string) => name.toLowerCase().includes(q) || id.toLowerCase().includes(q);
  const complexHit = new Set(q ? model.complexes.filter((complex) => hit(complex.name, complex.id)).map((complex) => complex.key) : []);
  const objectHit = new Set(q ? model.objects.filter((item) => complexHit.has(item.complexKey) || hit(item.name, item.id)).map((item) => item.key) : []);
  const complexShown = new Set(q ? model.complexes.filter((complex) => complexHit.has(complex.key) || complex.members.some((item) => objectHit.has(item.key))).map((complex) => complex.key) : []);
  const dimObject = (item: MapObject) => q !== "" && !objectHit.has(item.key) && item.key !== selectedKey;
  const dimComplex = (complex: MapComplex) => q !== "" && !complexShown.has(complex.key) && complex.key !== selectedKey;

  const selectedComplex = model.complexes.find((complex) => complex.key === selectedKey && complex.feature) ?? null;
  const selectedObject = selectedComplex ? null : model.objects.find((item) => item.key === selectedKey) ?? null;
  const selectedFeature = selectedComplex?.feature ?? selectedObject?.feature ?? null;
  const ownerOfSelected = selectedObject ? model.complexes.find((complex) => complex.key === selectedObject.complexKey) ?? null : null;
  const selectedForecasts = useMemo(() => {
    if (!selectedComplex && !selectedObject) return [];
    return forecasts.filter((item) => selectedComplex ? item.object.complex_id === selectedComplex.id : item.object.id === selectedObject?.id).sort((a, b) => a.rank - b.rank);
  }, [forecasts, selectedComplex, selectedObject]);

  const element = (key: string) => canvasRef.current?.querySelector<SVGGElement>(`[data-key="${CSS.escape(key)}"]`) ?? null;
  function select(key: string, reveal = false) {
    setSelectedKey(key);
    requestAnimationFrame(() => {
      if (reveal) element(key)?.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
      // В одну колонку паспорт стоит под схемой, за краем экрана: без прокрутки клик
      // по точке выглядел так, будто ничего не произошло.
      else if (window.matchMedia("(max-width: 1200px)").matches) panelRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    });
  }
  function close() {
    const key = selectedKey;
    setSelectedKey(null);
    if (key) requestAnimationFrame(() => element(key)?.focus());
  }
  function changeZoom(next: number) {
    const el = canvasRef.current;
    if (el) pendingCenter.current = (el.scrollLeft + (el.clientWidth - layout.labelW) / 2) / layout.plotW;
    setZoom(next);
  }
  const zoomIndex = ZOOMS.indexOf(zoom);
  const activate = (key: string) => (event: KeyboardEvent) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(key); } };
  function onSearchKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter" || !q) return;
    event.preventDefault();
    // Точное имя комплекса — сам комплекс; иначе первый объект, чьё имя совпало; иначе первый найденный комплекс.
    const exact = model.complexes.find((item) => item.feature && (item.name.toLowerCase() === q || item.id.toLowerCase() === q));
    const key = exact?.key ?? model.objects.find((item) => hit(item.name, item.id))?.key ?? model.complexes.find((item) => item.feature && complexHit.has(item.key))?.key;
    if (key) select(key, true);
  }

  const total = num(collection.properties.objects) ?? model.objects.length;
  const complexesTotal = num(collection.properties.complexes) ?? model.complexes.filter((complex) => complex.feature).length;
  const openTotal = num(collection.properties.open_forecasts) ?? (riskState === "ok" ? forecasts.length : null);
  const unplaced = num(collection.properties.channels_without_picket);
  const found = model.objects.filter((item) => objectHit.has(item.key)).length;
  const foundComplexes = complexShown.size;
  const { narrow, labelW, plotW, height, rows, ticks } = layout;

  if (model.complexes.length === 0 && model.objects.length === 0) return <StateView state="empty" />;
  return <div className="network-layout" onKeyDown={(event) => { if (event.key === "Escape" && selectedKey) close(); }}>
    <div className="network-map panel">
      <div className="map-toolbar">
        <div className="map-toolbar__main"><div className="map-search"><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onSearchKey} placeholder="Найти объект" aria-label="Поиск объекта или комплекса на схеме" title="Enter — выбрать первый найденный" /></div>
        <div className="map-zoom" role="group" aria-label="Масштаб по пикетам">
          <button type="button" onClick={() => changeZoom(ZOOMS[zoomIndex - 1])} disabled={zoomIndex <= 0} aria-label="Уменьшить масштаб" title="Уменьшить">−</button>
          <button type="button" className="map-zoom__value" onClick={() => changeZoom(1)} disabled={zoom === 1} title="Вписать схему по ширине" aria-label={`Масштаб ${fmtPercent(zoom)}, вписать по ширине`}>{fmtPercent(zoom)}</button>
          <button type="button" onClick={() => changeZoom(ZOOMS[zoomIndex + 1])} disabled={zoomIndex >= ZOOMS.length - 1} aria-label="Увеличить масштаб" title="Увеличить">+</button>
        </div></div>
        <div className="map-legend" aria-hidden="true"><span><i className="dot dot--risk" />Открытый прогноз</span><span><i className="dot" />Объект</span><span><i className="line-dot" />Комплекс</span></div>
      </div>
      {riskState !== "ok" && <div className="map-risk-state" role="status">{riskState === "loading" ? "Загружаем открытые прогнозы…" : "Не удалось загрузить открытые прогнозы."}{riskState === "error" && <button type="button" onClick={onRiskRetry}>Повторить</button>}</div>}
      {q && found === 0 && foundComplexes === 0 && <div className="map-risk-state" role="status">Ничего не найдено по запросу «{query.trim()}».<button type="button" onClick={() => setQuery("")}>Сбросить поиск</button></div>}
      <div className="map-canvas" ref={canvasRef}>
        <div className="map-stage" style={{ width: labelW + plotW, height }}>
          <div className={`map-labels${narrow ? " map-labels--narrow" : ""}`} style={{ width: labelW, height }} aria-hidden="true">
            {!narrow && <span className="map-labels__head" style={{ height: AXIS_H }}>Комплекс</span>}
            {rows.map((row, index) => { const complex = row.complex, feature = complex.feature; return <div key={complex.key} className={`map-label-row${index % 2 ? " map-label-row--alt" : ""}`} style={{ top: row.top, height: row.height }}>
              <span className={`map-label${selectedKey === complex.key || ownerOfSelected === complex ? " map-label--selected" : ""}${dimComplex(complex) ? " map-label--dim" : ""}${feature ? "" : " map-label--static"}`} style={narrow ? undefined : { top: row.lineY - row.top }} title={complex.name} onClick={feature ? () => select(complex.key) : undefined}><span className="map-label__name">{complex.name}</span>{complex.count ? <b>{complex.count}</b> : null}</span>
            </div>; })}
          </div>
          <svg width={plotW} height={height} role="group" aria-label="Условная схема сети: по горизонтали пикеты, строка — комплекс. Объект выбирается клавишей Enter или пробелом">
            {rows.map((row, index) => index % 2 ? <rect key={row.complex.key} className="map-row" x={0} y={row.top} width={plotW} height={row.height} /> : null)}
            {ticks.map((tick) => <g key={tick.value} className="map-tick"><line x1={tick.px} x2={tick.px} y1={AXIS_H - 6} y2={height} /><text x={tick.px} y={AXIS_H - 12} textAnchor="middle">ПК {fmtNumber(tick.value)}</text></g>)}
            {/* Строка — одна группа: Tab идёт по комплексу и его объектам подряд. Точки с открытым прогнозом
                рисуются последними: в SVG порядок — это слой, иначе область клика соседа по стопке перекрывала бы их. */}
            {rows.map((row) => { const complex = row.complex, x1 = Math.max(row.x1, row.x0 + 1); return <g key={complex.key}>
              {complex.feature && <g data-key={complex.key} className={`map-line${selectedKey === complex.key ? " map-line--selected" : ""}${dimComplex(complex) ? " map-dim" : ""}`} onClick={() => select(complex.key)} onKeyDown={activate(complex.key)} role="button" tabIndex={0} aria-label={`${selectedKey === complex.key ? "Выбран. " : ""}Комплекс ${complex.name}: ${complex.count === null ? "открытые прогнозы неизвестны" : counted(complex.count, "открытый прогноз", "открытых прогноза", "открытых прогнозов")}`}>
                <line className="map-line__hit" x1={row.x0} x2={x1} y1={row.lineY} y2={row.lineY} /><line className="map-line__stroke" x1={row.x0} x2={x1} y1={row.lineY} y2={row.lineY} /><title>{complex.name}</title>
              </g>}
              {[...row.dots].sort((a, b) => Number(a.risky) - Number(b.risky)).map((dot) => { const r = dot.risky ? 7 : 5, selected = selectedKey === dot.key; return <g key={dot.key} data-key={dot.key} className={`map-point${dot.risky ? " map-point--risk" : ""}${selected ? " map-point--selected" : ""}${dimObject(dot) ? " map-dim" : ""}`} transform={`translate(${dot.px} ${dot.py})`} onClick={() => select(dot.key)} onKeyDown={activate(dot.key)} role="button" tabIndex={0} aria-label={`${selected ? "Выбран. " : ""}${dot.name}: ${dot.count === null ? "открытые прогнозы неизвестны" : counted(dot.count, "открытый прогноз", "открытых прогноза", "открытых прогнозов")}`}>
                {dot.py !== row.lineY && <line className="map-point__stem" x1={0} x2={0} y1={0} y2={row.lineY - dot.py} />}
                <circle className="map-point__hit" r={dot.risky ? 9 : 7} />{dot.risky && <circle className="map-point__halo" r={13} />}<circle className="map-point__ring" r={r + 4} /><circle className="map-point__dot" r={r} />
                {dot.count !== null && dot.count > 0 && <text className="map-point__count" x={10} y={-8}>{dot.count}</text>}
                <title>{dot.name}: {dot.count === null ? "открытые прогнозы загружаются" : counted(dot.count, "открытый прогноз", "открытых прогноза", "открытых прогнозов")}</title>
              </g>; })}
            </g>; })}
          </svg>
        </div>
      </div>
      <div className="map-counter">{q ? (found || foundComplexes ? `Найдено: ${counted(found, "объект", "объекта", "объектов")} в ${counted(foundComplexes, "комплексе", "комплексах", "комплексах")}` : "Ничего не найдено") : <>{counted(total, "объект", "объекта", "объектов")} · {counted(complexesTotal, "комплекс", "комплекса", "комплексов")} · {openTotal === null ? "открытые прогнозы неизвестны" : counted(openTotal, "открытый прогноз", "открытых прогноза", "открытых прогнозов")}{unplaced !== null && unplaced > 0 && <span title="Канал без пикета не привязан к месту на схеме. Объект, у которого нет ни одного канала с пикетом, стоит в начале строки своего комплекса."> · {counted(unplaced, "канал", "канала", "каналов")} без пикета</span>}</>}</div>
    </div>
    <aside className="panel object-panel" ref={panelRef} aria-live="polite">{selectedFeature ? <Passport feature={selectedFeature} isComplex={selectedComplex !== null} count={(selectedComplex ?? selectedObject)?.count ?? null} complex={ownerOfSelected} members={selectedComplex?.members ?? []} forecasts={selectedForecasts} riskState={riskState} onSelectComplex={ownerOfSelected?.feature ? () => select(ownerOfSelected.key, true) : undefined} onClose={close} /> : <div className="object-panel__empty"><div className="map-pulse" /><h3>Выберите объект</h3><p>Нажмите на точку объекта или на линию комплекса — здесь появится паспорт: вид, пикеты, каналы и открытые прогнозы.</p></div>}</aside>
  </div>;
}

function Passport({ feature, isComplex, count, complex, members, forecasts, riskState, onSelectComplex, onClose }: { feature: Feature; isComplex: boolean; count: number | null; complex: MapComplex | null; members: MapObject[]; forecasts: Forecast[]; riskState: "ok" | "loading" | "error"; onSelectComplex?: () => void; onClose: () => void }) {
  const p = feature.properties;
  const id = text(p.id, "");
  const kind = text(p.kind, "");
  const source = p.source === "emulated" || p.source === "stub" ? p.source : null;
  const low = num(p.picket_min), high = num(p.picket_max);
  const channels = num(p.channels), withoutPicket = num(p.channels_without_picket) ?? 0;
  const riskyMembers = members.filter((item) => item.risky).length;
  const pickets = low === null ? (isComplex ? "нет каналов с пикетом" : "нет каналов с пикетом — точка стоит в начале строки комплекса") : low === high || high === null ? `ПК ${fmtNumber(low)}` : `ПК ${fmtNumber(low)}–${fmtNumber(high)}`;
  return <>
    <span className="panel__eyebrow">{isComplex ? "Комплекс" : "Объект"}</span>
    <h2>{text(p.name, id || "Объект")}{source && <> <SourceBadge source={source} /></>}</h2>
    <dl>
      <div><dt>Вид</dt><dd>{KIND_RU[kind] ?? (kind || "—")}</dd></div>
      {!isComplex && <div><dt>Комплекс</dt><dd>{complex?.feature && onSelectComplex ? <button type="button" className="object-panel__link" onClick={onSelectComplex}>{complex.name}</button> : complex?.name ?? "—"}</dd></div>}
      {isComplex && <div><dt>Объекты</dt><dd>{fmtNumber(members.length)}{riskyMembers > 0 ? `, с открытыми прогнозами ${fmtNumber(riskyMembers)}` : ""}</dd></div>}
      <div><dt>Пикеты</dt><dd>{pickets}</dd></div>
      <div><dt>Каналы</dt><dd>{channels === null ? "—" : fmtNumber(channels)}{withoutPicket > 0 ? `, из них без пикета ${fmtNumber(withoutPicket)}` : ""}</dd></div>
      <div><dt>Открытые прогнозы</dt><dd>{count === null ? "—" : fmtNumber(count)}</dd></div>
    </dl>
    {forecasts.length > 0 && <div className="object-panel__forecasts"><strong>Открытые прогнозы</strong>{forecasts.slice(0, 5).map((item) => <Link key={item.id} to={`/forecasts/${encodeURIComponent(item.id)}`}><span><span className="object-panel__title">{item.scenario_title}</span><small>{[isComplex ? item.object.name : null, item.channel?.picket_label, item.channel?.name].filter(Boolean).join(" · ") || "объект целиком"}</small></span><span className="object-panel__rank" title="Место в очереди дня">№ {item.rank} →</span></Link>)}{forecasts.length > 5 && <small>Показаны первые 5 из {fmtNumber(forecasts.length)}</small>}</div>}
    {count !== null && count > 0 && forecasts.length === 0 && riskState !== "ok" && <p className="object-panel__hint">{riskState === "loading" ? "Загружаем список прогнозов…" : "Список прогнозов не загрузился: «Повторить» — над схемой."}</p>}
    <div className="object-panel__actions">
      {!isComplex && id && <Link className="button" to={`/forecasts?obj=${encodeURIComponent(id)}`}>История прогнозов</Link>}
      <button className="button" type="button" onClick={onClose}>Закрыть</button>
    </div>
  </>;
}
