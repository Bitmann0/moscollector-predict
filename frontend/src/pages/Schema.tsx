import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Loaded, StateView } from "../components/StateView";

type Collection = Schemas["FeatureCollection"];
type Feature = Collection["features"][number];
type Forecast = Schemas["ForecastItem"];
type Point = [number, number];

function coordinates(feature: Feature): Point[] {
  const raw = feature.geometry.coordinates;
  if (!Array.isArray(raw)) return [];
  if (typeof raw[0] === "number" && typeof raw[1] === "number") return [[raw[0], raw[1]]];
  return raw.flat(2).reduce<Point[]>((all, value, index, flat) => { if (index % 2 === 0 && typeof value === "number" && typeof flat[index + 1] === "number") all.push([value, flat[index + 1] as number]); return all; }, []);
}
function text(value: unknown, fallback = "—"): string { return typeof value === "string" || typeof value === "number" ? String(value) : fallback; }

export function Schema() {
  const load = useLoad(() => api.GET("/api/v1/schema.geojson"), []);
  const forecasts = useLoad(() => api.GET("/api/v1/forecasts", { params: { query: { decision: "none", page: 1, page_size: 500 } } }), []);
  return <section><PageHeader eyebrow="Топология" title="Схема сети" description="Интерактивная карта комплексов, сооружений и открытых рисков" /><div className="map-note">Условная схема. Координаты заказчиком не предоставлены.</div><Loaded load={load}>{(collection) => <NetworkMap collection={collection} forecasts={forecasts.data?.items ?? []} forecastsTotal={forecasts.data?.total ?? 0} />}</Loaded></section>;
}

function NetworkMap({ collection, forecasts, forecastsTotal }: { collection: Collection; forecasts: Forecast[]; forecastsTotal: number }) {
  const [selected, setSelected] = useState<Feature | null>(null);
  const [query, setQuery] = useState("");
  const features = useMemo(() => collection.features.filter((feature) => text(feature.properties.name, "").toLowerCase().includes(query.toLowerCase()) || text(feature.properties.id, "").toLowerCase().includes(query.toLowerCase())), [collection.features, query]);
  const risksByObject = useMemo(() => { const result = new Map<string, number>(); for (const item of forecasts) { const id = item.object.id; if (id) result.set(id, (result.get(id) ?? 0) + 1); } return result; }, [forecasts]);
  const risksByComplex = useMemo(() => { const result = new Map<string, number>(); for (const item of forecasts) { const id = item.object.complex_id; if (id) result.set(id, (result.get(id) ?? 0) + 1); } return result; }, [forecasts]);
  const riskCount = (feature: Feature): number => { const id = text(feature.properties.id, ""); const complex = text(feature.properties.complex_id, text(feature.properties.complex_number, "")); return risksByObject.get(id) ?? risksByComplex.get(id) ?? risksByComplex.get(complex) ?? Number(feature.properties.open_forecasts ?? 0); };
  const allPoints = collection.features.flatMap(coordinates);
  if (allPoints.length === 0) return <StateView state="empty" />;
  const xs = allPoints.map(([x]) => x), ys = allPoints.map(([, y]) => y), minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const project = ([x, y]: Point): Point => [50 + (x - minX) / Math.max(1, maxX - minX) * 900, 40 + (y - minY) / Math.max(1, maxY - minY) * 500];
  const lines = features.filter((f) => String(f.geometry.type).includes("Line"));
  const points = features.filter((f) => String(f.geometry.type).includes("Point"));
  return <div className="network-layout">
    <div className="network-map panel"><div className="map-toolbar"><div className="map-search"><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Найти объект на схеме" /></div><div className="map-legend"><span><i className="dot dot--risk" />Открытый риск</span><span><i className="dot" />Объект</span><span><i className="line-dot" />Комплекс</span></div></div>
      <svg viewBox="0 0 1000 580" role="img" aria-label="Условная схема сети">
        <defs><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#e8eaf2" strokeWidth="1" /></pattern><filter id="glow"><feGaussianBlur stdDeviation="4" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs><rect width="1000" height="580" fill="url(#grid)" />
        {lines.map((feature, i) => { const pts = coordinates(feature).map(project); return <g key={`l-${i}`} onClick={() => setSelected(feature)} className="map-line"><polyline points={pts.map((p) => p.join(",")).join(" ")} /><text x={pts[Math.floor(pts.length / 2)]?.[0]} y={(pts[Math.floor(pts.length / 2)]?.[1] ?? 0) - 10}>{text(feature.properties.name, text(feature.properties.id, "Комплекс"))}</text></g>; })}
        {points.map((feature, i) => { const [point] = coordinates(feature).map(project); if (!point) return null; const count = riskCount(feature); const risky = count > 0 || ["high", "critical"].includes(text(feature.properties.risk_level, "")); return <g key={`p-${i}`} className={`map-point ${risky ? "map-point--risk" : ""} ${selected === feature ? "map-point--selected" : ""}`} transform={`translate(${point[0]} ${point[1]})`} onClick={() => setSelected(feature)}><circle r={risky ? 9 : 6} /><circle r={risky ? 17 : 12} className="map-point__halo" />{count > 0 && <text className="map-point__count" x="11" y="-10">{count}</text>}<title>{text(feature.properties.name, text(feature.properties.id, "Объект"))}: {count} открытых прогнозов</title></g>; })}
      </svg><div className="map-counter">{points.length} объектов · {lines.length} комплексов · {forecastsTotal} открытых прогнозов</div>
    </div>
    <aside className="panel object-panel">{selected ? <><span className="panel__eyebrow">Выбранный элемент</span><h2>{text(selected.properties.name, text(selected.properties.id, "Объект"))}</h2><dl><div><dt>Тип</dt><dd>{text(selected.properties.kind)}</dd></div><div><dt>Уровень</dt><dd>{text(selected.properties.level)}</dd></div><div><dt>Комплекс</dt><dd>{text(selected.properties.complex_id, text(selected.properties.complex_number))}</dd></div><div><dt>Открытых прогнозов</dt><dd>{riskCount(selected)}</dd></div><div><dt>Источник</dt><dd>{text(selected.properties.source)}</dd></div></dl>{riskCount(selected) > 0 && <Link className="button button--primary" to={`/forecasts?obj=${encodeURIComponent(text(selected.properties.id, ""))}`}>Открыть прогнозы</Link>} <button className="button" type="button" onClick={() => setSelected(null)}>Закрыть</button></> : <div className="object-panel__empty"><div className="map-pulse" /><h3>Выберите объект</h3><p>Нажмите на точку или линию, чтобы увидеть паспорт элемента инфраструктуры.</p></div>}</aside>
  </div>;
}
