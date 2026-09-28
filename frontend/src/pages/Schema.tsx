import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent } from "react";
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

// Каналы backend отдаёт только с фильтром по комплексу (всего их 11 485). На стенде
// 28.09 ответ по комплексу весил 16–472 КБ без сжатия, 323 байта на канал.
async function loadChannels(complexId: string): Promise<Channel[]> {
  const { data, response } = await api.GET("/api/v1/schema.geojson", { params: { query: { complex: complexId } } });
  if (!data || !response.ok) throw new Error(`schema.geojson?complex=${complexId}: ${response.status}`);
  return data.features.flatMap((feature) => {
    const p = feature.properties, id = num(p.id), picket = num(p.picket);
    if (p.feature !== "channel" || id === null || picket === null) return [];
    return [{ id, name: text(p.name, String(id)), sensorType: text(p.sensor_type), picket, objId: text(p.obj_id, ""), open: num(p.open_forecasts) ?? 0 }];
  });
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

// Слой каналов. На стенде в среднем 2,8 канала на пикет объекта, до 55 на одном, а пикет
// даже при 800 % на 1440 px занимает 4,1 px: канал отдельной точкой не виден. Поэтому
// точка — группа каналов объекта на соседних пикетах, шаг группы — не меньше BIN_PX.
// Слой включается с того масштаба, где точка покрывает не больше MAX_PICKETS пикетов:
// 300 % на 1440 px, 400 % на 1280 px, 600 % на телефоне 390 px.
const BIN_PX = 8;        // точка 6 px и зазор 2 px
const MAX_PICKETS = 6;
const TRACK = 11;        // дорожка каналов объекта под строкой комплекса
const TRACK_PAD = 6;
const VIEW_STEP = 64;    // окно видимости квантуется: прокрутка перерисовывает слой раз в 64 px
const TIP_LINES = 6;

interface Channel { id: number; name: string; sensorType: string; picket: number; objId: string; open: number }
type ChannelLoad = { status: "loading" } | { status: "error" } | { status: "ok"; items: Channel[] };
interface MapObject { key: string; feature: Feature; id: string; name: string; x: number; count: number | null; risky: boolean; complexKey: string; pmin: number | null; pmax: number | null }
interface MapComplex { key: string; feature: Feature | null; id: string; name: string; x0: number; x1: number; y: number; count: number | null; risky: boolean; members: MapObject[]; ownPlaced: number }
interface PlacedObject extends MapObject { px: number; py: number }
interface Track { key: string; objId: string; complexId: string; name: string; y: number; x0: number | null; x1: number | null }
interface Row { complex: MapComplex; top: number; height: number; lineY: number; x0: number; x1: number; dots: PlacedObject[]; tracks: Track[] }
interface Bin { key: string; track: Track; px: number; items: Channel[]; from: number; to: number; risky: boolean }
interface View { x0: number; x1: number; y0: number; y1: number }
interface Tip { bin: Bin; x: number; y: number; below: boolean }

const picketSpan = (from: number, to: number) => from === to ? `ПК ${fmtNumber(from)}` : `ПК ${fmtNumber(from)}–${fmtNumber(to)}`;

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
  const [channels, setChannels] = useState<Record<string, ChannelLoad>>({});
  const [view, setView] = useState<View | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);
  const [picked, setPicked] = useState<{ key: string; ids: number[] } | null>(null);
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
      return { key: `c:${id || index}`, feature, id, name: text(feature.properties.name, id || "Комплекс"), x0: Math.min(...lx), x1: Math.max(...lx), y: pts[0]?.[1] ?? 0, count, risky: risky(feature, count), members: [], ownPlaced: 0 };
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
      if (!owner) { orphans ??= { key: "c:none", feature: null, id: "", name: "Вне комплексов", x0: point[0], x1: point[0], y: -Infinity, count: null, risky: false, members: [], ownPlaced: 0 }; owner = orphans; }
      owner.members.push({ key: `o:${id || index}`, feature, id, name: text(feature.properties.name, id || "Объект"), x: point[0], count, risky: risky(feature, count), complexKey: owner.key, pmin: num(feature.properties.picket_min), pmax: num(feature.properties.picket_max) });
    });
    // Счётчики комплекса backend считает по комплексу вместе с объектами; разница с суммой
    // по объектам — каналы, привязанные к самому комплексу. Им нужна своя дорожка.
    for (const complex of complexes) {
      const p = complex.feature?.properties;
      if (!p) continue;
      const sum = (field: string) => complex.members.reduce((total, item) => total + (num(item.feature.properties[field]) ?? 0), 0);
      complex.ownPlaced = Math.max(0, (num(p.channels) ?? 0) - sum("channels") - ((num(p.channels_without_picket) ?? 0) - sum("channels_without_picket")));
    }
    if (orphans) complexes.push(orphans);
    return { complexes, xMin, xMax, objects: complexes.flatMap((complex) => complex.members) };
  }, [collection, forecasts, riskState]);

  const layout = useMemo(() => {
    const narrow = width > 0 && width < NARROW;
    const longest = Math.max(0, ...model.complexes.map((complex) => complex.name.length + (complex.count ? 4 : 0)));
    const labelW = narrow ? 0 : Math.round(Math.min(220, Math.max(120, longest * 6.8 + 28)));
    const plotAt = (z: number) => Math.max(280, Math.round(((width || 800) - labelW) * z));
    const scaleAt = (z: number) => (plotAt(z) - PAD_L - PAD_R) / Math.max(1, model.xMax - model.xMin);
    const plotW = plotAt(zoom), scale = scaleAt(zoom);
    const sx = (x: number) => PAD_L + (x - model.xMin) * scale;
    const channelZoom = ZOOMS.find((z) => scaleAt(z) * MAX_PICKETS >= BIN_PX) ?? null;
    const channelsOn = channelZoom !== null && zoom >= channelZoom;
    const binStep = Math.max(1, Math.ceil(BIN_PX / scale));
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
      // Дорожки каналов берутся из основного ответа (пикеты объекта), а не из подгруженных
      // каналов: высота строки не прыгает, когда каналы приходят.
      const owners = channelsOn && complex.feature && complex.id ? [
        ...(complex.ownPlaced > 0 ? [{ key: complex.key, objId: complex.id, name: "Каналы комплекса", x0: null, x1: null }] : []),
        ...complex.members.filter((item) => item.pmin !== null).map((item) => ({ key: item.key, objId: item.id, name: item.name, x0: sx(item.pmin ?? 0), x1: sx(item.pmax ?? item.pmin ?? 0) })),
      ] : [];
      const tracks: Track[] = owners.map((owner, index) => ({ ...owner, complexId: complex.id, y: top + band + inner + index * TRACK + TRACK / 2 }));
      const extra = tracks.length ? tracks.length * TRACK + TRACK_PAD : 0;
      const row: Row = { complex, top, height: band + inner + extra, lineY, x0: sx(complex.x0), x1: sx(complex.x1), dots: placed.map((p) => ({ ...p.item, px: p.px, py: lineY + p.lane * LANE })), tracks };
      top += band + inner + extra;
      return row;
    });
    const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000].find((value) => value * scale >= 80) ?? 10000;
    const ticks: { value: number; px: number }[] = [];
    for (let value = Math.ceil(model.xMin / step) * step; value <= model.xMax; value += step) ticks.push({ value, px: sx(value) });
    return { narrow, labelW, plotW, height: top + 8, rows, ticks, sx, channelZoom, channelsOn, binStep };
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

  // Окно видимости слоя каналов в координатах SVG с запасом в полэкрана. Прокрутку
  // страницы и холста ловим одним слушателем с capture: холст крутится сам по себе.
  const { channelsOn, labelW: viewLabelW } = layout;
  useLayoutEffect(() => {
    if (!channelsOn) { setView(null); setTip(null); return; }
    let frame = 0;
    const measure = () => {
      frame = 0;
      const el = canvasRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect(), w = el.clientWidth - viewLabelW, h = window.innerHeight;
      const down = (v: number) => Math.floor(v / VIEW_STEP) * VIEW_STEP, up = (v: number) => Math.ceil(v / VIEW_STEP) * VIEW_STEP;
      const next = { x0: down(el.scrollLeft - w / 2), x1: up(el.scrollLeft + w * 1.5), y0: down(-rect.top - h / 2), y1: up(h * 1.5 - rect.top) };
      setView((prev) => prev && prev.x0 === next.x0 && prev.x1 === next.x1 && prev.y0 === next.y0 && prev.y1 === next.y1 ? prev : next);
    };
    const onScroll = () => { setTip(null); if (!frame) frame = requestAnimationFrame(measure); };
    const onResize = () => { if (!frame) frame = requestAnimationFrame(measure); };
    measure();
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", onResize);
    return () => { cancelAnimationFrame(frame); window.removeEventListener("scroll", onScroll, { capture: true }); window.removeEventListener("resize", onResize); };
  }, [channelsOn, viewLabelW, layout.plotW, layout.height]);

  // Каналы грузятся по комплексу, когда его строка попадает в окно, и остаются в памяти.
  useEffect(() => {
    if (!channelsOn || !view) return;
    for (const row of layout.rows) {
      const id = row.complex.id;
      if (!row.tracks.length || channels[id] || row.top + row.height < view.y0 || row.top > view.y1) continue;
      setChannels((prev) => ({ ...prev, [id]: { status: "loading" } }));
      loadChannels(id).then((items) => setChannels((prev) => ({ ...prev, [id]: { status: "ok", items } })), () => setChannels((prev) => ({ ...prev, [id]: { status: "error" } })));
    }
  }, [channelsOn, view, layout.rows, channels]);

  const forecastsByChannel = useMemo(() => {
    const out = new Map<number, Forecast[]>();
    for (const item of forecasts) { const id = item.channel?.id; if (id !== undefined) out.set(id, [...(out.get(id) ?? []), item]); }
    return out;
  }, [forecasts]);
  const channelsByObj = useMemo(() => {
    const out = new Map<string, Channel[]>();
    for (const load of Object.values(channels)) if (load.status === "ok") for (const item of load.items) out.set(item.objId, [...(out.get(item.objId) ?? []), item]);
    for (const list of out.values()) list.sort((a, b) => a.picket - b.picket || a.id - b.id);
    return out;
  }, [channels]);
  const channelRisky = useCallback((item: Channel) => item.open > 0 || forecastsByChannel.has(item.id), [forecastsByChannel]);

  // Точка слоя — каналы объекта в полосе из binStep пикетов; полосы выровнены по xMin,
  // поэтому соседние точки не ближе BIN_PX при любом масштабе.
  const bins = useMemo(() => {
    const out: Bin[] = [];
    if (!layout.channelsOn) return out;
    const { binStep, sx } = layout;
    for (const row of layout.rows) for (const track of row.tracks) {
      let bin: Bin | null = null, index = -1;
      for (const item of channelsByObj.get(track.objId) ?? []) {
        const k = Math.floor((item.picket - model.xMin) / binStep);
        if (!bin || k !== index) { index = k; bin = { key: `${track.key}|${k}`, track, px: sx(model.xMin + k * binStep + (binStep - 1) / 2), items: [], from: item.picket, to: item.picket, risky: false }; out.push(bin); }
        bin.items.push(item);
        bin.to = item.picket;
        if (channelRisky(item)) bin.risky = true;
      }
    }
    return out;
  }, [layout, channelsByObj, channelRisky, model.xMin]);
  const binByKey = useMemo(() => new Map(bins.map((bin) => [bin.key, bin])), [bins]);
  const extent = useMemo(() => {
    const out = new Map<string, [number, number]>();
    for (const bin of bins) { const prev = out.get(bin.track.key); out.set(bin.track.key, prev ? [Math.min(prev[0], bin.px), Math.max(prev[1], bin.px)] : [bin.px, bin.px]); }
    return out;
  }, [bins]);

  const q = query.trim().toLowerCase();
  const hit = useCallback((name: string, id: string) => name.toLowerCase().includes(q) || id.toLowerCase().includes(q), [q]);
  const { complexHit, objectHit, complexShown } = useMemo(() => {
    const complexHit = new Set(q ? model.complexes.filter((complex) => hit(complex.name, complex.id)).map((complex) => complex.key) : []);
    const objectHit = new Set(q ? model.objects.filter((item) => complexHit.has(item.complexKey) || hit(item.name, item.id)).map((item) => item.key) : []);
    const complexShown = new Set(q ? model.complexes.filter((complex) => complexHit.has(complex.key) || complex.members.some((item) => objectHit.has(item.key))).map((complex) => complex.key) : []);
    return { complexHit, objectHit, complexShown };
  }, [q, hit, model]);
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
  function select(key: string, reveal = false, channelIds: number[] | null = null) {
    setSelectedKey(key);
    setPicked(channelIds ? { key, ids: channelIds } : null);
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
    setPicked(null);
    if (key) requestAnimationFrame(() => element(key)?.focus());
  }
  // Точки каналов не в порядке Tab: при 400 % на 1440 px в окне их 520. С клавиатуры
  // каналы с открытыми прогнозами доступны списком прогнозов в паспорте объекта.
  const binOf = useCallback((target: EventTarget) => { const el = (target as Element).closest?.("[data-bin]"), bin = el ? binByKey.get(el.getAttribute("data-bin") ?? "") : undefined; return el && bin ? { el, bin } : null; }, [binByKey]);
  const selectRef = useRef(select);
  selectRef.current = select;
  const onChannelClick = useCallback((event: MouseEvent) => { const found = binOf(event.target); if (found) selectRef.current(found.bin.track.key, false, found.bin.items.map((item) => item.id)); }, [binOf]);
  // Подсказка — один div на весь слой, а не <title> у каждой точки: системная подсказка
  // появляется с задержкой браузера и не вмещает список каналов группы.
  const onChannelOver = useCallback((event: PointerEvent) => {
    const found = binOf(event.target);
    if (!found) return;
    const rect = found.el.getBoundingClientRect(), half = Math.min(150, (window.innerWidth - 24) / 2), below = rect.top < 200;
    setTip({ bin: found.bin, x: Math.min(Math.max(rect.left + rect.width / 2, half + 12), window.innerWidth - half - 12), y: below ? rect.bottom : rect.top, below });
  }, [binOf]);
  const onChannelOut = useCallback((event: PointerEvent) => { if (!(event.relatedTarget as Element | null)?.closest?.("[data-bin]")) setTip(null); }, []);
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
  const { narrow, labelW, plotW, height, rows, ticks, channelZoom, binStep } = layout;

  const dimTrack = useCallback((track: Track) => q !== "" && track.key !== selectedKey && !objectHit.has(track.key) && !complexHit.has(track.key), [q, selectedKey, objectHit, complexHit]);
  // Слой каналов собран в useMemo: подсказка меняет состояние на каждой точке, и без
  // этого каждый наведённый канал перерисовывал бы все точки слоя.
  const channelLayer = useMemo(() => {
    if (!channelsOn) return null;
    const pickedIds = new Set(picked?.ids ?? []);
    const visible = view ? bins.filter((bin) => bin.px >= view.x0 && bin.px <= view.x1 && bin.track.y >= view.y0 && bin.track.y <= view.y1) : [];
    return <g className="map-channels" aria-hidden="true" onClick={onChannelClick} onPointerOver={onChannelOver} onPointerOut={onChannelOut}>
      {rows.flatMap((row) => row.tracks).map((track) => { const span = track.x0 !== null && track.x1 !== null ? [track.x0, track.x1] : extent.get(track.key); return span ? <line key={track.key} className={`map-track${track.key === selectedKey ? " map-track--selected" : ""}${dimTrack(track) ? " map-dim" : ""}`} x1={span[0]} x2={Math.max(span[1], span[0] + 1)} y1={track.y} y2={track.y} /> : null; })}
      {visible.map((bin) => {
        const cls = `map-channel${bin.risky ? " map-channel--risk" : ""}${dimTrack(bin.track) ? " map-dim" : ""}`;
        const isPicked = picked?.key === bin.track.key && bin.items.some((item) => pickedIds.has(item.id));
        if (!bin.risky && !isPicked) return <circle key={bin.key} data-bin={bin.key} className={cls} cx={bin.px} cy={bin.track.y} r={3} />;
        return <g key={bin.key} data-bin={bin.key} className={cls} transform={`translate(${bin.px} ${bin.track.y})`}>{bin.risky && <circle className="map-channel__halo" r={8} />}<circle r={bin.risky ? 4 : 3} />{isPicked && <circle className="map-channel__ring" r={bin.risky ? 7 : 6} />}</g>;
      })}
    </g>;
  }, [channelsOn, picked, view, bins, rows, extent, selectedKey, dimTrack, onChannelClick, onChannelOver, onChannelOut]);
  const channelStatus = useMemo(() => {
    const visible = rows.filter((row) => row.tracks.length && view && row.top + row.height >= view.y0 && row.top <= view.y1);
    return { loading: visible.some((row) => channels[row.complex.id]?.status === "loading"), failed: rows.filter((row) => row.tracks.length && channels[row.complex.id]?.status === "error").map((row) => row.complex) };
  }, [rows, view, channels]);
  const retryChannels = () => setChannels((prev) => Object.fromEntries(Object.entries(prev).filter(([, load]) => load.status !== "error")));
  const pickedChannels = useMemo(() => {
    if (!picked || picked.key !== selectedKey) return [];
    const ids = new Set(picked.ids);
    return [...channelsByObj.values()].flat().filter((item) => ids.has(item.id)).sort((a, b) => Number(channelRisky(b)) - Number(channelRisky(a)) || a.picket - b.picket || a.id - b.id);
  }, [picked, selectedKey, channelsByObj, channelRisky]);
  const tipItems = tip ? [...tip.bin.items].sort((a, b) => Number(channelRisky(b)) - Number(channelRisky(a)) || a.picket - b.picket || a.id - b.id) : [];

  if (model.complexes.length === 0 && model.objects.length === 0) return <StateView state="empty" />;
  return <div className="network-layout" onKeyDown={(event) => { if (event.key === "Escape" && selectedKey) close(); }}>
    <div className="network-map panel">
      <div className="map-toolbar">
        <div className="map-toolbar__main"><div className="map-search"><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={onSearchKey} placeholder="Найти объект" aria-label="Поиск объекта или комплекса на схеме" title="Enter — выбрать первый найденный" /></div>
        <div className="map-zoom" role="group" aria-label="Масштаб по пикетам">
          <button type="button" onClick={() => changeZoom(ZOOMS[zoomIndex - 1])} disabled={zoomIndex <= 0} aria-label="Уменьшить масштаб" title="Уменьшить">−</button>
          <button type="button" className="map-zoom__value" onClick={() => changeZoom(1)} disabled={zoom === 1} title="Вписать схему по ширине" aria-label={`Масштаб ${fmtPercent(zoom)}, вписать по ширине`}>{fmtPercent(zoom)}</button>
          <button type="button" onClick={() => changeZoom(ZOOMS[zoomIndex + 1])} disabled={zoomIndex >= ZOOMS.length - 1} aria-label="Увеличить масштаб" title={channelZoom !== null && zoom < channelZoom ? `Увеличить. Каналы видны с ${fmtPercent(channelZoom)}` : "Увеличить"}>+</button>
        </div></div>
        <div className="map-legend" aria-hidden="true"><span><i className="dot dot--risk" />Открытый прогноз</span><span><i className="dot" />Объект</span><span><i className="line-dot" />Комплекс</span>{channelsOn ? <span><i className="dot dot--channel" />Каналы: точка на {counted(binStep, "пикет", "пикета", "пикетов")}</span> : channelZoom !== null && <span className="map-legend__hint">Каналы — с {fmtPercent(channelZoom)}</span>}</div>
      </div>
      {riskState !== "ok" && <div className="map-risk-state" role="status">{riskState === "loading" ? "Загружаем открытые прогнозы…" : "Не удалось загрузить открытые прогнозы."}{riskState === "error" && <button type="button" onClick={onRiskRetry}>Повторить</button>}</div>}
      {q && found === 0 && foundComplexes === 0 && <div className="map-risk-state" role="status">Ничего не найдено по запросу «{query.trim()}».<button type="button" onClick={() => setQuery("")}>Сбросить поиск</button></div>}
      {channelStatus.failed.length > 0 && <div className="map-risk-state" role="status">Не загрузились каналы: {channelStatus.failed.map((complex) => complex.name).join(", ")}.<button type="button" onClick={retryChannels}>Повторить</button></div>}
      <div className="map-canvas" ref={canvasRef}>
        <div className="map-stage" style={{ width: labelW + plotW, height }}>
          <div className={`map-labels${narrow ? " map-labels--narrow" : ""}`} style={{ width: labelW, height }} aria-hidden="true">
            {!narrow && <span className="map-labels__head" style={{ height: AXIS_H }}>Комплекс</span>}
            {rows.map((row, index) => { const complex = row.complex, feature = complex.feature; return <div key={complex.key} className={`map-label-row${index % 2 ? " map-label-row--alt" : ""}`} style={{ top: row.top, height: row.height }}>
              <span className={`map-label${selectedKey === complex.key || ownerOfSelected === complex ? " map-label--selected" : ""}${dimComplex(complex) ? " map-label--dim" : ""}${feature ? "" : " map-label--static"}`} style={narrow ? undefined : { top: row.lineY - row.top }} title={complex.name} onClick={feature ? () => select(complex.key) : undefined}><span className="map-label__name">{complex.name}</span>{complex.count ? <b>{complex.count}</b> : null}</span>
              {!narrow && row.tracks.map((track) => <span key={track.key} className={`map-track-label${track.key === selectedKey ? " map-track-label--selected" : ""}${dimTrack(track) ? " map-label--dim" : ""}`} style={{ top: track.y - row.top }} title={track.name} onClick={() => select(track.key)}>{track.name}</span>)}
            </div>; })}
          </div>
          <svg width={plotW} height={height} role="group" aria-label="Условная схема сети: по горизонтали пикеты, строка — комплекс. Объект выбирается клавишей Enter или пробелом">
            {rows.map((row, index) => index % 2 ? <rect key={row.complex.key} className="map-row" x={0} y={row.top} width={plotW} height={row.height} /> : null)}
            {ticks.map((tick) => <g key={tick.value} className="map-tick"><line x1={tick.px} x2={tick.px} y1={AXIS_H - 6} y2={height} /><text x={tick.px} y={AXIS_H - 12} textAnchor="middle">ПК {fmtNumber(tick.value)}</text></g>)}
            {channelLayer}
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
      <div className="map-counter">{q ? (found || foundComplexes ? `Найдено: ${counted(found, "объект", "объекта", "объектов")} в ${counted(foundComplexes, "комплексе", "комплексах", "комплексах")}` : "Ничего не найдено") : <>{counted(total, "объект", "объекта", "объектов")} · {counted(complexesTotal, "комплекс", "комплекса", "комплексов")} · {openTotal === null ? "открытые прогнозы неизвестны" : counted(openTotal, "открытый прогноз", "открытых прогноза", "открытых прогнозов")}{unplaced !== null && unplaced > 0 && <span title="Канал без пикета не привязан к месту на схеме. Объект, у которого нет ни одного канала с пикетом, стоит в начале строки своего комплекса."> · {counted(unplaced, "канал", "канала", "каналов")} без пикета</span>}{channelStatus.loading && " · загружаем каналы…"}</>}</div>
      {tip && <div className={`map-tip${tip.below ? " map-tip--below" : ""}`} style={{ left: tip.x, top: tip.y }} role="tooltip">
        <strong>{tip.bin.track.name}</strong>
        <small>{picketSpan(tip.bin.from, tip.bin.to)} · {counted(tip.bin.items.length, "канал", "канала", "каналов")}</small>
        {tipItems.slice(0, TIP_LINES).map((item) => <span key={item.id}>{channelRisky(item) && <i />}<b>{item.name}</b> · {item.sensorType} · ПК {fmtNumber(item.picket)}</span>)}
        {tipItems.length > TIP_LINES && <small>и ещё {fmtNumber(tipItems.length - TIP_LINES)}</small>}
      </div>}
    </div>
    <aside className="panel object-panel" ref={panelRef} aria-live="polite">{selectedFeature ? <Passport feature={selectedFeature} isComplex={selectedComplex !== null} count={(selectedComplex ?? selectedObject)?.count ?? null} complex={ownerOfSelected} members={selectedComplex?.members ?? []} forecasts={selectedForecasts} riskState={riskState} picked={pickedChannels} forecastsByChannel={forecastsByChannel} channelRisky={channelRisky} onSelectComplex={ownerOfSelected?.feature ? () => select(ownerOfSelected.key, true) : undefined} onClose={close} /> : <div className="object-panel__empty"><div className="map-pulse" /><h3>Выберите объект</h3><p>Нажмите на точку объекта или на линию комплекса — здесь появится паспорт: вид, пикеты, каналы и открытые прогнозы.</p></div>}</aside>
  </div>;
}

const PICKED_SHOWN = 8;

function Passport({ feature, isComplex, count, complex, members, forecasts, riskState, picked, forecastsByChannel, channelRisky, onSelectComplex, onClose }: { feature: Feature; isComplex: boolean; count: number | null; complex: MapComplex | null; members: MapObject[]; forecasts: Forecast[]; riskState: "ok" | "loading" | "error"; picked: Channel[]; forecastsByChannel: Map<number, Forecast[]>; channelRisky: (item: Channel) => boolean; onSelectComplex?: () => void; onClose: () => void }) {
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
    {picked.length > 0 && <div className="object-panel__channels"><strong>{picked.length === 1 ? "Канал" : counted(picked.length, "канал", "канала", "каналов")} на {picketSpan(Math.min(...picked.map((item) => item.picket)), Math.max(...picked.map((item) => item.picket)))}</strong>
      {picked.slice(0, PICKED_SHOWN).map((item) => { const list = forecastsByChannel.get(item.id) ?? [], risky = channelRisky(item); return <div key={item.id} className={`object-panel__channel${risky ? " object-panel__channel--risk" : ""}`}>
        <span className="object-panel__title">{item.name}</span><small>{item.sensorType} · ПК {fmtNumber(item.picket)}</small>
        {list.map((forecast) => <Link key={forecast.id} to={`/forecasts/${encodeURIComponent(forecast.id)}`}>{forecast.scenario_title} · № {forecast.rank} →</Link>)}
        {risky && list.length === 0 && <small>{counted(item.open, "открытый прогноз", "открытых прогноза", "открытых прогнозов")}</small>}
      </div>; })}
      {picked.length > PICKED_SHOWN && <small>Показаны первые {PICKED_SHOWN} из {fmtNumber(picked.length)}, сначала с открытыми прогнозами</small>}
    </div>}
    <dl>
      <div><dt>Вид</dt><dd>{KIND_RU[kind] ?? (kind || "—")}</dd></div>
      {!isComplex && <div><dt>Комплекс</dt><dd>{complex?.feature && onSelectComplex ? <button type="button" className="object-panel__link" onClick={onSelectComplex}>{complex.name}</button> : complex?.name ?? "—"}</dd></div>}
      {isComplex && <div><dt>Объекты</dt><dd>{fmtNumber(members.length)}{riskyMembers > 0 ? `, с открытыми прогнозами ${fmtNumber(riskyMembers)}` : ""}</dd></div>}
      <div><dt>Пикеты</dt><dd>{pickets}</dd></div>
      <div><dt>Каналы</dt><dd>{channels === null ? "—" : fmtNumber(channels)}</dd></div>
      {withoutPicket > 0 && <div><dt>Без пикета</dt><dd>{fmtNumber(withoutPicket)} — места на схеме у них нет</dd></div>}
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
