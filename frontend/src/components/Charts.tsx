/**
 * Линейные и столбчатые графики рабочего места.
 *
 * Сетка, подписи осей, линия лимита и точки считаются от одной шкалы и
 * позиционируются в процентах одной и той же области, поэтому ничего не
 * съезжает при любой ширине. Линии рисует SVG с preserveAspectRatio="none" и
 * non-scaling-stroke: он тянется по контейнеру, а толщина линии остаётся 2 px.
 * Наведение и стрелки клавиатуры показывают значения ближайшего дня.
 *
 * Пропуск (null) линию рвёт, а не соединяет соседей: иначе на графике появлялось
 * бы значение, которого никто не замерял. Счётчики за день рисуются столбцами —
 * у них нет промежуточных значений между днями, которые подразумевает линия.
 */
import { useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type MouseEvent } from "react";

import { fmtDate } from "../format";

export interface ChartPoint { day: string; value: number | null }

export interface ChartSeries {
  key: string;
  label: string;
  color: string;
  values: (number | null)[];
  dashed?: boolean;
  area?: boolean;
  /** Столбцы вместо линии: для величин, которые считаются за день целиком. */
  bars?: boolean;
}

interface Scale { max: number; ticks: number[] }

/**
 * Круглые шаги оси: 1, 2, 2,5, 5 × 10ⁿ, не больше четырёх интервалов.
 * integer — ряд из счётчиков: шаг не меньше 1, иначе при одной тревоге в сутки
 * ось подписала бы «0,25» и «0,75» тревоги.
 */
export function niceScale(maxValue: number, percent: boolean, integer = false): Scale {
  if (percent) return { max: 1, ticks: [0, 0.25, 0.5, 0.75, 1] };
  const top = Math.max(maxValue, 1);
  const raw = top / 4;
  const power = 10 ** Math.floor(Math.log10(raw));
  const steps = [1, 2, 2.5, 5, 10].map((m) => m * power)
    .filter((s) => !integer || (s >= 1 && Number.isInteger(s)));
  const step = steps.find((s) => s >= raw) ?? (integer ? Math.max(1, Math.ceil(raw)) : raw);
  const max = Math.ceil(top / step) * step;
  const ticks: number[] = [];
  for (let value = 0; value <= max + step / 2; value += step) ticks.push(Math.round(value * 1000) / 1000);
  return { max, ticks };
}

/**
 * Дневной ряд без дыр в календаре: пропущенный день между первым и последним
 * становится null. Иначе ось по индексу сжимала бы время и соединяла дни через пропуск.
 */
export function dailySeries(points: ChartPoint[]): ChartPoint[] {
  const parse = (day: string) => { const [y, m, d] = day.slice(0, 10).split("-").map(Number); return Date.UTC(y, m - 1, d); };
  const known = new Map(points.map((p) => [p.day.slice(0, 10), p.value]));
  const sorted = [...known.keys()].sort();
  if (sorted.length < 2) return points;
  const out: ChartPoint[] = [];
  for (let t = parse(sorted[0]), end = parse(sorted[sorted.length - 1]); t <= end; t += 86_400_000) {
    const day = new Date(t).toISOString().slice(0, 10);
    out.push({ day, value: known.get(day) ?? null });
  }
  return out;
}

function tickText(value: number, percent: boolean): string {
  return percent ? `${Math.round(value * 100)} %` : value.toLocaleString("ru-RU");
}

function valueText(value: number | null, percent: boolean, gapLabel: string): string {
  if (value === null) return gapLabel;
  return percent ? `${Math.round(value * 100)} %` : value.toLocaleString("ru-RU");
}

/**
 * Отрезки подряд идущих известных значений: по ним рисуются линии и заливки.
 * null — «нет данных», поэтому линия через него не проводится: иначе разрыв
 * читался бы как значения, которых никто не наблюдал.
 */
function runs(values: (number | null)[]): number[][] {
  const out: number[][] = [];
  let current: number[] = [];
  values.forEach((v, i) => {
    if (v === null || v === undefined) { if (current.length) out.push(current); current = []; }
    else current.push(i);
  });
  if (current.length) out.push(current);
  return out;
}

export function LineChart({ days, series, percent = false, limit, height = 180, label, gapLabel = "нет данных" }: {
  days: string[];
  series: ChartSeries[];
  percent?: boolean;
  limit?: { value: number; label: string };
  height?: number;
  label: string;
  /** Подпись пропуска в подсказке: что значит отсутствие значения за день. */
  gapLabel?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const plotRef = useRef<HTMLDivElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  // Подсказка не выходит за область графика: иначе у середины узкой карточки она
  // вылезала на соседнюю панель, а на телефоне — за край экрана. Ширину подсказки
  // знает только браузер, поэтому место считаем после отрисовки, до показа кадра.
  useLayoutEffect(() => {
    const plot = plotRef.current, tip = tipRef.current;
    if (!plot || !tip) return;
    const width = plot.clientWidth, own = tip.offsetWidth, anchor = (Number(tip.dataset.anchor) / 100) * width;
    let left = anchor + 12;
    if (left + own > width) left = anchor - 12 - own;
    if (left < 0) left = Math.max(0, Math.min(width - own, anchor - own / 2));
    tip.style.left = `${left}px`;
    tip.style.transform = "none";
  });
  const count = days.length;
  if (count === 0 || series.every((s) => s.values.every((v) => v === null))) {
    return <div className="chart-empty" style={{ height }}>Нет данных для графика</div>;
  }
  // Столбцы занимают полосу дня, точки линии — её середину; крайние дни не срезаются.
  const band = series.some((s) => s.bars);
  const values = series.flatMap((s) => s.values.filter((v): v is number => v !== null));
  const integer = !percent && [...values, limit?.value ?? 0].every(Number.isInteger);
  const scale = niceScale(Math.max(...values, limit?.value ?? 0), percent, integer);
  const x = (index: number) => (band ? ((index + 0.5) / count) * 100 : count === 1 ? 50 : (index / (count - 1)) * 100);
  const y = (value: number) => 100 - (Math.min(value, scale.max) / scale.max) * 100;
  const points = (s: ChartSeries, run: number[]) => run.map((i) => `${x(i)},${y(s.values[i] as number)}`).join(" ");
  const barWidth = (100 / count) * 0.62;
  const gaps = band ? days.map((_, i) => i).filter((i) => series.every((s) => s.values[i] === null || s.values[i] === undefined)) : [];

  function pick(clientX: number, rect: DOMRect) {
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    if (band) setHover(Math.min(count - 1, Math.floor(ratio * count)));
    else setHover(count === 1 ? 0 : Math.round(ratio * (count - 1)));
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
        ref={plotRef}
        className="line-chart__plot"
        role="img"
        tabIndex={0}
        aria-label={`${label}: ${count} ${band ? "дней" : "точек"}, ${fmtDate(days[0])} — ${fmtDate(days[count - 1])}. Стрелки влево и вправо показывают значения`}
        onMouseMove={(event: MouseEvent<HTMLDivElement>) => pick(event.clientX, event.currentTarget.getBoundingClientRect())}
        onMouseLeave={() => setHover(null)}
        onBlur={() => setHover(null)}
        onKeyDown={onKey}
      >
        {scale.ticks.map((tick) => <i key={tick} className={`line-chart__grid${tick === 0 ? " line-chart__grid--base" : ""}`} style={{ top: `${y(tick)}%` }} />)}
        {gaps.map((i) => <i key={`gap-${i}`} className="line-chart__gap" style={{ left: `${(i / count) * 100}%`, width: `${100 / count}%` }} />)}
        {hover !== null && band && <i className="line-chart__band" style={{ left: `${(hover / count) * 100}%`, width: `${100 / count}%` }} />}
        {limit && (
          <div className="line-chart__limit" style={{ top: `${y(limit.value)}%` }}>
            <span>{limit.label} {valueText(limit.value, percent, gapLabel)}</span>
          </div>
        )}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          {series.map((s) => s.bars && s.values.map((v, i) => v !== null && v !== undefined && v > 0 && (
            <rect key={`${s.key}-${i}`} x={x(i) - barWidth / 2} y={y(v)} width={barWidth} height={100 - y(v)} fill={s.color} opacity={hover === null || hover === i ? 1 : 0.55} />
          )))}
          {series.map((s) => !s.bars && s.area && runs(s.values).filter((run) => run.length > 1).map((run) => (
            <polygon key={`${s.key}-area-${run[0]}`} points={`${x(run[0])},100 ${points(s, run)} ${x(run[run.length - 1])},100`} fill={s.color} opacity="0.12" />
          )))}
          {series.map((s) => !s.bars && runs(s.values).filter((run) => run.length > 1).map((run) => (
            <polyline key={`${s.key}-${run[0]}`} points={points(s, run)} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" strokeDasharray={s.dashed ? "6 4" : undefined} vectorEffect="non-scaling-stroke" />
          )))}
        </svg>
        {/* Одиночное значение между пропусками линией не нарисовать — показываем точкой. */}
        {series.map((s) => !s.bars && runs(s.values).filter((run) => run.length === 1).map(([i]) => (
          <b key={`${s.key}-pt-${i}`} className="line-chart__point" style={{ left: `${x(i)}%`, top: `${y(s.values[i] as number)}%`, background: s.color }} />
        )))}
        {hover !== null && (
          <>
            {!band && <i className="line-chart__cursor" style={{ left: `${tipLeft}%` }} />}
            {series.map((s) => !s.bars && s.values[hover] !== null && s.values[hover] !== undefined && (
              <b key={s.key} className="line-chart__dot" style={{ left: `${tipLeft}%`, top: `${y(s.values[hover] as number)}%`, background: s.color }} />
            ))}
            <div ref={tipRef} data-anchor={tipLeft} className={`line-chart__tip${tipLeft > 60 ? " line-chart__tip--left" : ""}`} style={{ left: `${tipLeft}%` }}>
              <strong>{fmtDate(days[hover])}</strong>
              {series.map((s) => (
                <span key={s.key}><i style={{ background: s.color }} />{s.label}: {valueText(s.values[hover] ?? null, percent, gapLabel)}</span>
              ))}
            </div>
          </>
        )}
      </div>
      {/* Подпись стоит под своей точкой или столбцом; крайние прижаты к краям области, чтобы не вылезать за неё. */}
      <div className="line-chart__x" aria-hidden="true">
        <span className="line-chart__x-first">{fmtDate(days[0])}</span>
        {middle !== null && <span style={{ left: `${x(middle)}%` }}>{fmtDate(days[middle])}</span>}
        {count > 1 && <span className="line-chart__x-last">{fmtDate(days[count - 1])}</span>}
      </div>
    </figure>
  );
}

/** Одна серия по дням — обёртка над LineChart для дашборда и «Качества». */
export function TrendChart({ data, limit, limitLabel = "лимит", percent = false, color = "var(--data)", label = "Динамика по дням", seriesLabel = "Значение", bars = false, gapLabel }: {
  data: ChartPoint[];
  limit?: number;
  limitLabel?: string;
  percent?: boolean;
  color?: string;
  label?: string;
  seriesLabel?: string;
  bars?: boolean;
  gapLabel?: string;
}) {
  return (
    <LineChart
      days={data.map((p) => p.day)}
      series={[{ key: "value", label: seriesLabel, color, values: data.map((p) => p.value), area: !bars, bars }]}
      percent={percent}
      limit={limit === undefined ? undefined : { value: limit, label: limitLabel }}
      label={label}
      gapLabel={gapLabel}
    />
  );
}

export function Donut({ value, label }: { value: number | null; label: string }) {
  const safe = Math.min(1, Math.max(0, value ?? 0));
  const text = value === null ? "—" : `${Math.round(safe * 100)} %`;
  return <div className="donut" role="img" aria-label={`${label}: ${value === null ? "нет данных" : text}`} style={{ "--progress": `${safe * 360}deg` } as CSSProperties}><div aria-hidden="true"><strong>{text}</strong><span>{label}</span></div></div>;
}
