import { Link } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { TrendChart, Donut } from "../components/Charts";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { SourceBadge } from "../components/common";
import { Loaded, StateView, headState } from "../components/StateView";
import { fmtDate, fmtPercent, placeText, scoreText } from "../format";
import { title } from "../vocab";

type Summary = Schemas["DashboardSummary"];
type Forecast = Schemas["ForecastItem"];

export function Dashboard() {
  const summary = useLoad(() => api.GET("/api/v1/dashboard/summary"), []);
  const queue = useLoad(() => api.GET("/api/v1/forecasts", { params: { query: { page: 1, page_size: 6 } } }), []);
  return <section>
    <PageHeader eyebrow="Оперативный контур" title="Центр управления" description="Риски инфраструктуры и действия диспетчерской службы в одном окне" actions={<Link className="button button--primary" to="/forecasts">Открыть очередь <Icon name="arrow" /></Link>} />
    <Loaded load={summary}>{(data) => <SummaryView summary={data} queue={queue.data?.items ?? []} />}</Loaded>
  </section>;
}

function SummaryView({ summary, queue }: { summary: Summary; queue: Forecast[] }) {
  const open = summary.scenarios.reduce((sum, item) => sum + item.open_forecasts, 0);
  const coverage = summary.scenarios.map((item) => item.coverage_fraction).filter((item): item is number => item !== null && item !== undefined);
  const meanCoverage = coverage.length ? coverage.reduce((sum, item) => sum + item, 0) / coverage.length : null;
  const activeOrders = Object.entries(summary.work_orders_by_status).filter(([status]) => !["completed", "cancelled"].includes(status)).reduce((sum, [, count]) => sum + count, 0);
  return <>
    <div className="context-line"><span>Срез на {fmtDate(summary.demo_today)}</span><SourceBadge source={summary.source} /></div>
    <div className="metric-grid">
      <Metric icon="forecast" tone="violet" value={open} label="Открытых прогнозов" detail="Требуют внимания" />
      <Metric icon="shield" tone="blue" value={fmtPercent(meanCoverage)} label="Средний охват" detail="Объектов в расчёте" />
      <Metric icon="wrench" tone="mint" value={activeOrders} label="Активных заявок" detail="Превентивные работы" />
      <Metric icon="events" tone="amber" value={summary.alarms_24h} label="Тревог за 24 часа" detail={`${summary.planned_like_alarms_24h} похожи на плановые`} />
    </div>
    <div className="dashboard-layout">
      <div className="dashboard-main">
        <div className="scenario-grid">
          {summary.scenarios.map((item) => {
            const head = summary.heads.find((value) => value.scenario === item.scenario);
            const state = head ? headState(head) : null;
            return <article className="panel scenario-card" key={item.scenario}><div><span className="panel__eyebrow">Сценарий</span><h3>{item.title}</h3><p><b>{item.open_forecasts}</b> прогнозов в очереди</p>{state && <StateView state={state} detail={head?.detail} compact />}</div><Donut value={item.coverage_fraction ?? null} label="охват" /></article>;
          })}
        </div>
        <div className="chart-grid">
          <article className="panel chart-card"><header><div><span className="panel__eyebrow">Нагрузка</span><h3>Прогнозы по дням</h3></div><span className="legend"><i />Выдано</span></header><TrendChart data={summary.series_forecasts_per_day} limit={20} /></article>
          <article className="panel chart-card"><header><div><span className="panel__eyebrow">Данные</span><h3>Охват мониторинга</h3></div><span className="legend legend--blue"><i />Доля объектов</span></header><TrendChart data={summary.series_coverage_per_day} percent color="var(--blue)" /></article>
        </div>
      </div>
      <aside className="panel priority-panel"><header><div><span className="panel__eyebrow">Приоритет</span><h3>Очередь диспетчера</h3></div><Link to="/forecasts">Все</Link></header>{queue.length === 0 ? <StateView state="empty" /> : <ol className="priority-list">{queue.map((item) => <li key={item.id}><span className="rank">{item.rank}</span><div><Link to={`/forecasts/${encodeURIComponent(item.id)}`}>{placeText(item)}</Link><small>{item.scenario_title} · {scoreText(item)}</small></div><Icon name="arrow" /></li>)}</ol>}<div className="queue-footer"><Icon name="activity" /><span>Очередь пересчитывается автоматически после поступления новых данных</span></div></aside>
    </div>
    <div className="panel system-strip"><div><span className="live-dot" /><strong>Сценарии под контролем</strong></div>{summary.heads.map((head) => { const state = headState(head); return <span key={head.head}>{title("scenario", head.scenario)} {state && <StateView state={state} compact />}</span>; })}</div>
  </>;
}

function Metric({ icon, tone, value, label, detail }: { icon: Parameters<typeof Icon>[0]["name"]; tone: string; value: string | number; label: string; detail: string }) {
  return <article className={`metric-card metric-card--${tone}`}><div className="metric-card__icon"><Icon name={icon} /></div><div><strong>{value}</strong><span>{label}</span><small>{detail}</small></div></article>;
}
