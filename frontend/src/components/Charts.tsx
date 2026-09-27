/**
 * Линейные графики рабочего места.
 *
 * Сетка, подписи оси, линия лимита и точки считаются от одной шкалы и
 * позиционируются в процентах одной и той же области, поэтому ничего не
 * съезжает при любой ширине. Линии рисует SVG с preserveAspectRatio="none" и
 * non-scaling-stroke: он тянется по контейнеру, а толщина линии остаётся 2 px.
 * Наведение и стрелки клавиатуры показывают значения ближайшего дня.
 */
import { useState, type CSSProperties, type KeyboardEvent, type MouseEvent } from "react";

import { fmtDate } from "../format";

export interface ChartPoint { day: string; value: number }

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
  values: (number | null)[];
  dashed?: boolean;
  area?: boolean;
}

interface Scale { max: number; ticks: number[] }

/** Круглые шаги оси: 1, 2, 2,5, 5 × 10ⁿ, не больше четырёх интервалов. */
export function niceScale(maxValue: number, percent: boolean): Scale {
  if (percent) return { max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
  const top = Math.max(maxValue, 1);
  const raw = top / 4;
  const power = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => s >= raw) ?? raw;
  const max = Math.ceil(top / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= max + step / 2; value += step) ticks.push(Math.round(value * 1000) / 1000);
  return { max, ticks };
}

function tickText(value: number, percent: boolean): string {
  return percent ? `${Math.round(value * 100)} %` : value.toLocaleString("ru-RU");
}

function valueText(value: number | null, percent: boolean): string {
  if (value === null) return "нет данных";
  return percent ? `${Math.round(value * 100)} %` : value.toLocaleString("ru-RU");
}

export function LineChart({ days, series, percent = false, limit, height = 180, label }: {
  days: string[];
  series: ChartSeries[];
  percent?: boolean;
  limit?: { value: number; label: string };
  height?: number;
  label: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const count = days.length;
  if (count === 0 || series.every((s) => s.values.every((v) => v === null))) {
    return <div className="chart-empty" style={{ height }}>Нет данных для графика</div>;
  }
  const values = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const scale = niceScale(Math.max(...values, limit?.value ?? 0), percent);
  const x = (index: number) => (count === 1 ? 50 : (index / (count - 1)) * 100);
  const y = (value: number) => 100 - (Math.min(value, scale.max) / scale.max) * 100;
  const path = (s: ChartSeries) => s.values.map((v, i) => (v === null ? null : `${x(i)},${y(v)}`)).filter(Boolean).join(" ");

  function pick(clientX: number, rect: DOMRect) {
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    setHover(count === 1 ? 0 : Math.round(ratio * (count - 1)));
  }
  function onKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const step = event.key === "ArrowLeft" ? -1 : 1;
    setHover((current) => Math.min(count - 1, Math.max(0, (current ?? (step > 0 ? -1 : count)) + step)));
  }
  const middle = count > 2 ? Math.floor((count - 1) / 2) : null;
  const tipLeft = hover === null ? 0 : x(hover);

  return (
    <figure className="line-chart" style={{ "--chart-h": `${height}px` } as CSSProperties}>
      <div className="line-chart__y" aria-hidden="true">
        {scale.ticks.map((tick) => <span key={tick} style={{ top: `${y(tick)}%` }}>{tickText(tick, percent)}</span>)}
      </div>
      <div
        className="line-chart__plot"
        role="img"
        tabIndex={0}
        aria-label={`${label}: ${count} точек, ${fmtDate(days[0])} — ${fmtDate(days[count - 1])}. Стрелки влево и вправо показывают значения`}
        onMouseMove={(event: MouseEvent<HTMLDivElement>) => pick(event.clientX, event.currentTarget.getBoundingClientRect())}
        onMouseLeave={() => setHover(null)}
        onBlur={() => setHover(null)}
        onKeyDown={onKey}
      >
        {scale.ticks.map((tick) => <i key={tick} className={`line-chart__grid${tick === 0 ? " line-chart__grid--base" : ""}`} style={{ top: `${y(tick)}%` }} />)}
        {limit && (
          <div className="line-chart__limit" style={{ top: `${y(limit.value)}%` }}>
            <span>{limit.label} {valueText(limit.value, percent)}</span>
          </div>
        )}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          {series.map((s) => s.area && (
            <polygon key={`${s.key}-area`} points={`${x(0)},100 ${path(s)} ${x(count - 1)},100`} fill={s.color} opacity="0.12" />
          ))}
          {series.map((s) => (
            <polyline key={s.key} points={path(s)} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" strokeDasharray={s.dashed ? "6 4" : undefined} vectorEffect="non-scaling-stroke" />
          ))}
        </svg>
        {hover !== null && (
          <>
            <i className="line-chart__cursor" style={{ left: `${tipLeft}%` }} />
            {series.map((s) => s.values[hover] !== null && s.values[hover] !== undefined && (
              <b key={s.key} className="line-chart__dot" style={{ left: `${tipLeft}%`, top: `${y(s.values[hover] as number)}%`, background: s.color }} />
            ))}
            <div className={`line-chart__tip${tipLeft > 60 ? " line-chart__tip--left" : ""}`} style={{ left: `${tipLeft}%` }}>
              <strong>{fmtDate(days[hover])}</strong>
              {series.map((s) => (
                <span key={s.key}><i style={{ background: s.color }} />{s.label}: {valueText(s.values[hover] ?? null, percent)}</span>
              ))}
            </div>
          </>
        )}
      </div>
      <div className="line-chart__x" aria-hidden="true">
        <span>{fmtDate(days[0])}</span>
        {middle !== null && <span>{fmtDate(days[middle])}</span>}
        {count > 1 && <span>{fmtDate(days[count - 1])}</span>}
      </div>
    </figure>
  );
}

/** Одна серия по дням — обёртка над LineChart для дашборда и «Качества». */
export function TrendChart({ data, limit, limitLabel = "лимит", percent = false, color = "var(--data)", label = "Динамика по дням", seriesLabel = "Значение" }: {
  data: ChartPoint[];
  limit?: number;
  limitLabel?: string;
  percent?: boolean;
  color?: string;
  label?: string;
  seriesLabel?: string;
}) {
  return (
    <LineChart
      days={data.map((p) => p.day)}
      series={[{ key: "value", label: seriesLabel, color, values: data.map((p) => p.value), area: true }]}
      percent={percent}
      limit={limit === undefined ? undefined : { value: limit, label: limitLabel }}
      label={label}
    />
  );
}

export function Donut({ value, label }: { value: number | null; label: string }) {
  const safe = Math.min(1, Math.max(0, value ?? 0));
  return <div className="donut" style={{ "--progress": `${safe * 360}deg` } as CSSProperties}><div><strong>{value === null ? "—" : `${Math.round(safe * 100)} %`}</strong><span>{label}</span></div></div>;
}
