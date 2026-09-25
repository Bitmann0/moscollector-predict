/**
 * ЗАГЛУШКА — владелец FE-02 (C2). Заменить: плоский список KPI на дашборд —
 * карточки KPI, две диаграммы с осью от нуля (прогнозы по дням с линией лимита,
 * охват по дням), топ очереди; библиотеку диаграмм выбирает FE-02.
 * Контракт: типы из src/api/schema.d.ts (DashboardSummary, GET /api/v1/dashboard/summary);
 * npm run typecheck должен остаться зелёным.
 */
import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { SourceBadge } from "../components/common";
import { Loaded, StateView, headState } from "../components/StateView";
import { fmtDate, fmtNumber, fmtPercent } from "../format";
import { title } from "../vocab";

type Summary = Schemas["DashboardSummary"];

export function Dashboard() {
  const load = useLoad(() => api.GET("/api/v1/dashboard/summary"), []);
  return (
    <section>
      <h1>Обзор</h1>
      <Loaded load={load}>{(summary) => <SummaryView summary={summary} />}</Loaded>
    </section>
  );
}

function SummaryView({ summary }: { summary: Summary }) {
  const orders = Object.entries(summary.work_orders_by_status);
  return (
    <>
      <p className="muted">
        На {fmtDate(summary.demo_today)} <SourceBadge source={summary.source} />
      </p>
      <dl className="kpi">
        {summary.scenarios.map((kpi) => (
          <div key={kpi.scenario} className="kpi__row">
            <dt>{kpi.title}</dt>
            <dd>
              открытых прогнозов: {kpi.open_forecasts}; охват: {fmtPercent(kpi.coverage_fraction)}
            </dd>
          </div>
        ))}
        <div className="kpi__row">
          <dt>Заявки</dt>
          <dd>
            {orders.length === 0
              ? "нет"
              : orders.map(([status, count]) => `${title("work_order_status", status)}: ${count}`).join("; ")}
          </dd>
        </div>
        <div className="kpi__row">
          <dt>Тревожных событий за 24 часа</dt>
          <dd>{summary.alarms_24h}</dd>
        </div>
        <div className="kpi__row">
          <dt>Из них похожих на плановые работы</dt>
          <dd>{summary.planned_like_alarms_24h}</dd>
        </div>
        {summary.heads.map((head) => {
          const state = headState(head);
          return (
            <div key={head.head} className="kpi__row">
              <dt>Статус: {title("scenario", head.scenario)}</dt>
              <dd>
                {state ? <StateView state={state} detail={head.detail} compact /> : "расчёта ещё не было"}
              </dd>
            </div>
          );
        })}
      </dl>
      <DailySeries summary={summary} />
    </>
  );
}

/** Два ряда по дням в одной таблице: FE-02 заменит её диаграммами. */
function DailySeries({ summary }: { summary: Summary }) {
  const days = new Map<string, { forecasts?: number; coverage?: number }>();
  for (const point of summary.series_forecasts_per_day) {
    days.set(point.day, { ...days.get(point.day), forecasts: point.value });
  }
  for (const point of summary.series_coverage_per_day) {
    days.set(point.day, { ...days.get(point.day), coverage: point.value });
  }
  const rows = [...days.entries()].sort(([a], [b]) => b.localeCompare(a));
  if (rows.length === 0) return null;
  return (
    <>
      <h2>По дням</h2>
      <table className="table table--narrow">
        <thead>
          <tr>
            <th>День</th>
            <th className="num">Прогнозов</th>
            <th className="num">Охват</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([day, row]) => (
            <tr key={day}>
              <td>{fmtDate(day)}</td>
              <td className="num">{fmtNumber(row.forecasts)}</td>
              <td className="num">{fmtPercent(row.coverage)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
