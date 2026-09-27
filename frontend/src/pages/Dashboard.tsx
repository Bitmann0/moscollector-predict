import { Link } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad, type Load } from "../api/useLoad";
import { TrendChart, Donut } from "../components/Charts";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { SourceBadge } from "../components/common";
import { Loaded, StateView, headState } from "../components/StateView";
import { fmtDate, fmtPercent, placeText, scoreText } from "../format";
import { useReloadOn } from "../stream/useStream";
import { title } from "../vocab";

type Summary = Schemas["DashboardSummary"];
type Forecast = Schemas["ForecastItem"];

async function loadDispatcherQueue(): Promise<{ data?: Forecast[]; error?: unknown; response: Response }> {
  const queue: Forecast[] = [];
  let page = 1;
  while (true) {
    const result = await api.GET("/api/v1/forecasts", { params: { query: { decision: "none", page, page_size: 100 } } });
    if (!result.data || !result.response.ok) return { error: result.error, response: result.response };
    queue.push(...result.data.items.filter((item) => !item.decision).slice(0, 6 - queue.length));
    if (queue.length >= 6 || page * 100 >= result.data.total) return { data: queue, response: result.response };
    page += 1;
  }
}

export function Dashboard() {
  const summary = useLoad(() => api.GET("/api/v1/dashboard/summary"), []);
  const queue = useLoad(loadDispatcherQueue, []);
  useReloadOn(["run.finished", "alert.new"], summary.reload);
  useReloadOn(["run.finished", "alert.new"], queue.reload);
  return <section>
    <PageHeader eyebrow="Оперативный контур" title="Центр управления" description="Риски инфраструктуры и действия диспетчерской службы в одном окне" actions={<><button className="button" type="button" onClick={() => { summary.reload(); queue.reload(); }}>Обновить</button><Link className="button button--primary" to="/forecasts">Открыть очередь <Icon name="arrow" /></Link></>} />
    <Loaded load={summary}>{(data) => <SummaryView summary={data} queue={queue} />}</Loaded>
  </section>;
}

function SummaryView({ summary, queue }: { summary: Summary; queue: Load<Forecast[]> }) {
  const open = summary.scenarios.reduce((sum, item) => sum + item.open_forecasts, 0);
  const coverage = summary.scenarios.map((item) => item.coverage_fraction).filter((item): item is number => item !== null && item !== undefined);
  const meanCoverage = coverage.length ? coverage.reduce((sum, item) => sum + item, 0) / coverage.length : null;
  const activeOrders = Object.entries(summary.work_orders_by_status).filter(([status]) => !["completed", "cancelled"].includes(status)).reduce((sum, [, count]) => sum + count, 0);
  return <>
    <div className="context-line"><span>Срез на {fmtDate(summary.demo_today)}</span><SourceBadge source={summary.source} /></div>
    <div className="metric-grid">
      <Metric icon="forecast" tone="violet" value={open} label="Открытых прогнозов" detail="Требуют внимания" />
      <Metric icon="shield" tone="blue" value={fmtPercent(meanCoverage)} label="Охват расчёта" detail="Доля каналов, среднее по сценариям" />
      <Metric icon="wrench" tone="mint" value={activeOrders} label="Активных заявок" detail="Превентивные работы" />
      <Metric icon="events" tone="amber" value={summary.alarms_24h} label="Тревог за 24 часа" detail={`${summary.planned_like_alarms_24h} похожи на плановые`} />
    </div>
    <div className="dashboard-layout">
      <div className="dashboard-main">
        <div className="scenario-grid">
          {summary.scenarios.map((item) => {
            const head = summary.heads.find((value) => value.scenario === item.scenario);
            const state = head ? headState(head) : null;
            return <article className={`panel scenario-card scenario-card--${item.scenario}`} key={item.scenario}><h3 title={item.title}>{item.title}</h3><div className="scenario-card__body"><div><p><b>{item.open_forecasts}</b> {plural(item.open_forecasts, "прогноз", "прогноза", "прогнозов")} в очереди</p>{state && <StateView state={state} detail={head?.detail} compact />}</div><Donut value={item.coverage_fraction ?? null} label="охват" /></div></article>;
          })}
        </div>
        <div className="chart-grid">
          <article className="panel chart-card"><header><div><span className="panel__eyebrow">Нагрузка</span><h3>Прогнозы по дням</h3></div><span className="legend"><i />Выдано</span></header><TrendChart data={summary.series_forecasts_per_day} label="Прогнозы по дням" seriesLabel="Выдано" /></article>
          <article className="panel chart-card"><header><div><span className="panel__eyebrow">Данные</span><h3>Охват мониторинга</h3></div><span className="legend legend--blue"><i />Доля каналов</span></header><TrendChart data={summary.series_coverage_per_day} percent label="Охват мониторинга" seriesLabel="Доля каналов" /></article>
        </div>
      </div>
      <aside className="panel priority-panel"><header><div><span className="panel__eyebrow">Приоритет</span><h3>Очередь диспетчера</h3></div><Link to="/forecasts">Все</Link></header><div className="priority-panel__body"><Loaded load={queue}>{(items) => items.length === 0 ? <StateView state="empty" detail="Прогнозов без решения сейчас нет." /> : <ol className="priority-list">{items.map((item) => <li key={item.id}><span className="rank">{item.rank}</span><div><Link to={`/forecasts/${encodeURIComponent(item.id)}`}>{placeText(item)}</Link><small>{item.scenario_title} · {scoreText(item)}</small></div><Icon name="arrow" /></li>)}</ol>}</Loaded></div><div className="queue-footer"><Icon name="activity" /><span>Очередь обновляется после нового расчёта или по кнопке «Обновить»</span></div></aside>
    </div>
    <div className="panel orders-overview"><div><Icon name="orders"/><span><strong>Заявки по статусам</strong><small>Операционная загрузка службы эксплуатации</small></span></div><div>{Object.entries(summary.work_orders_by_status).map(([status,count]) => <Link key={status} to={`/work-orders?status=${encodeURIComponent(status)}`}><strong>{count}</strong><span>{title("work_order_status", status)}</span></Link>)}</div><Link className="orders-overview__all" to="/work-orders">Открыть все <Icon name="arrow"/></Link></div>
  </>;
}

function Metric({ icon, tone, value, label, detail }: { icon: Parameters<typeof Icon>[0]["name"]; tone: string; value: string | number; label: string; detail: string }) {
  return <article className={`metric-card metric-card--${tone}`}><div className="metric-card__icon"><Icon name={icon} /></div><div><strong>{value}</strong><span>{label}</span><small>{detail}</small></div></article>;
}

function plural(count: number, one: string, few: string, many: string): string {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}
