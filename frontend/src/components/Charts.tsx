import type { CSSProperties } from "react";
import { fmtDate } from "../format";

export interface ChartPoint { day: string; value: number }

function pointsOf(data: ChartPoint[], width: number, height: number, max: number): string {
  if (data.length === 0) return "";
  const dx = data.length === 1 ? 0 : width / (data.length - 1);
  return data.map((point, index) => `${index * dx},${height - (point.value / max) * height}`).join(" ");
}

export function TrendChart({ data, limit, percent = false, color = "var(--accent)" }: { data: ChartPoint[]; limit?: number; percent?: boolean; color?: string }) {
  if (data.length === 0) return <div className="chart-empty">Нет данных для графика</div>;
  const width = 640;
  const height = 180;
  const values = data.map((point) => point.value);
  const max = percent ? 1 : Math.max(...values, limit ?? 0, 1) * 1.12;
  const line = pointsOf(data, width, height, max);
  const area = `0,${height} ${line} ${width},${height}`;
  const limitY = limit === undefined ? null : height - (limit / max) * height;
  const ticks = [max, max / 2, 0];
  return (
    <div className="trend-chart">
      <div className="trend-chart__axis" aria-hidden="true">
        {ticks.map((tick) => <span key={tick}>{percent ? `${Math.round(tick * 100)}%` : Math.round(tick)}</span>)}
      </div>
      <svg viewBox={`-4 -8 ${width + 8} ${height + 22}`} role="img" aria-label={`Динамика: ${data.length} точек`}>
        {[0, height / 2, height].map((y) => <line key={y} x1="0" x2={width} y1={y} y2={y} className="chart-guide" />)}
        <defs><linearGradient id={`area-${percent ? "percent" : "count"}`} x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor={color} stopOpacity=".28"/><stop offset="1" stopColor={color} stopOpacity="0"/></linearGradient></defs>
        <polygon points={area} fill={`url(#area-${percent ? "percent" : "count"})`} />
        {limitY !== null && <><line x1="0" x2={width} y1={limitY} y2={limitY} className="chart-limit"/><text x={width - 4} y={limitY - 6} textAnchor="end" className="chart-label">лимит {limit}</text></>}
        <polyline points={line} fill="none" stroke={color} strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" />
        {data.map((point, index) => {
          const x = data.length === 1 ? 0 : index * width / (data.length - 1);
          const y = height - point.value / max * height;
          return <circle key={point.day} cx={x} cy={y} r="4" fill={color}><title>{fmtDate(point.day)}: {percent ? `${Math.round(point.value * 100)}%` : point.value}</title></circle>;
        })}
      </svg>
      <div className="trend-chart__dates"><span>{fmtDate(data[0].day)}</span><span>{fmtDate(data[data.length - 1].day)}</span></div>
    </div>
  );
}

export function Donut({ value, label }: { value: number | null; label: string }) {
  const safe = Math.min(1, Math.max(0, value ?? 0));
  return <div className="donut" style={{ "--progress": `${safe * 360}deg` } as CSSProperties}><div><strong>{value === null ? "—" : `${Math.round(safe * 100)}%`}</strong><span>{label}</span></div></div>;
}
