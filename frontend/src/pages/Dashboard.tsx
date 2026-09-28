import { Link } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad, type Load } from "../api/useLoad";
import { TrendChart, Donut, dailySeries } from "../components/Charts";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { SourceBadge } from "../components/common";
import { Loaded, StateView, headState } from "../components/StateView";
import { fmtDate, fmtNumber, placeText, scoreText } from "../format";
import { useReloadOn } from "../stream/useStream";
import { title } from "../vocab";

type Summary = Schemas["DashboardSummary"];
type Forecast = Schemas["ForecastItem"];

const QUEUE_SIZE = 6;

/**
 * Верх очереди диспетчера: прогнозы без решения, окно которых не кончилось к
 * полуночи demo_today. Правило то же, что у счётчика открытых прогнозов на
 * backend (helpers.open_forecast_clauses): без него в «очередь» попадали
 * прогнозы с закрытым окном, по которым уже поздно что-то делать, а число
 * строк расходилось с KPI «Открытых прогнозов».
 */
async function loadDispatcherQueue(demoToday: string | undefined): Promise<{ data?: Forecast[]; error?: unknown; response: Response }> {
  // Пока нет сводки, неизвестна и дата среза: ждём, запрос уйдёт при её появлении.
  if (!demoToday) return new Promise(() => {});
  const since = Date.parse(`${demoToday}T00:00:00+03:00`);
  const queue: Forecast[] = [];
  let page = 1;
  while (true) {
    const result = await api.GET("/api/v1/forecasts", { params: { query: { decision: "none", page, page_size: 500 } } });
    if (!result.data || !result.response.ok) return { error: result.error, response: result.response };
    const open = result.data.items.filter((item) => !item.decision && Date.parse(item.valid_to) >= since);
    queue.push(...open.slice(0, QUEUE_SIZE - queue.length));
    if (queue.length >= QUEUE_SIZE || page * 500 >= result.data.total) return { data: queue, response: result.response };
    page += 1;
  }
}

export function Dashboard() {
  const summary = useLoad(() => api.GET("/api/v1/dashboard/summary"), []);
  const demoToday = summary.data?.demo_today;
  const queue = useLoad(() => loadDispatcherQueue(demoToday), [demoToday]);
  // Заявки меняются и с других экранов: без перезапроса сводка по статусам отставала бы.
  useReloadOn(["run.finished", "alert.new", "workorder.changed"], summary.reload);
  useReloadOn(["run.finished", "alert.new"], queue.reload);
  return <section className="dashboard-page">
    <PageHeader eyebrow="Оперативный контур" title="Центр управления" description="Риски инфраструктуры и действия диспетчерской службы в одном окне" actions={<><button className="button" type="button" onClick={() => { summary.reload(); queue.reload(); }}>Обновить</button><Link className="button button--primary" to="/forecasts">Открыть очередь <Icon name="arrow" /></Link></>} />
    <Loaded load={summary}>{(data) => <SummaryView summary={data} queue={queue} />}</Loaded>
  </section>;
}

function SummaryView({ summary, queue }: { summary: Summary; queue: Load<Forecast[]> }) {
  const open = summary.scenarios.reduce((sum, item) => sum + item.open_forecasts, 0);
  const coverage = summary.scenarios.map((item) => item.coverage_fraction).filter((item): item is number => item !== null && item !== undefined);
  const meanCoverage = coverage.length ? coverage.reduce((sum, item) => sum + item, 0) / coverage.length : null;
  const activeOrders = Object.entries(summary.work_orders_by_status).filter(([status]) => !["completed", "cancelled"].includes(status)).reduce((sum, [, count]) => sum + count, 0);
  // Сценарий без охвата (недельная очередь его не считает) в среднее не входит — так и пишем.
  const coverageDetail = coverage.length === 0 ? "Нет данных об охвате" : coverage.length === summary.scenarios.length ? "Доля каналов, среднее по сценариям" : `Доля каналов, среднее по ${coverage.length} из ${summary.scenarios.length} сценариев`;
  return <>
    <div className="context-line"><span>Срез на {fmtDate(summary.demo_today)}</span><SourceBadge source={summary.source} /></div>
    <div className="metric-grid">
      <Metric icon="forecast" tone="violet" value={open} label="Открытых прогнозов" detail="Требуют внимания" />
      {/* Целые проценты — как в кольцах сценариев и на графике охвата: одно число не пишется двумя способами. */}
      <Metric icon="shield" tone="blue" value={percentText(meanCoverage)} label="Охват расчёта" detail={coverageDetail} />
      <Metric icon="wrench" tone="mint" value={activeOrders} label="Активных заявок" detail="Превентивные работы" />
      {/* Янтарь — сигнал; при нуле тревог он подсвечивал бы то, чего нет. */}
      <Metric icon="events" tone={summary.alarms_24h > 0 ? "amber" : "neutral"} value={summary.alarms_24h} label="Тревожных сообщений за 24 часа" detail={`${summary.planned_like_alarms_24h} похожи на ППР или ТО`} />
    </div>
    <div className="dashboard-layout">
      <div className="dashboard-main">
        <div className="dashboard-scenarios">
          <div className="scenario-grid">
            {summary.scenarios.map((item) => {
              const head = summary.heads.find((value) => value.scenario === item.scenario);
              const state = head ? headState(head) : null;
              return <article className={`panel scenario-card scenario-card--${item.scenario}`} key={item.scenario}>
                <h3 title={item.title}><Link to={`/scenarios/${item.scenario}`}>{item.title}</Link></h3>
                <p className="scenario-card__count"><b>{item.open_forecasts}</b> <span>{plural(item.open_forecasts, "прогноз", "прогноза", "прогнозов")} в очереди</span></p>
                <Donut value={item.coverage_fraction ?? null} label="охват" />
                {state && <div className="scenario-card__state"><StateView state={state} detail={head?.detail} compact /></div>}
              </article>;
            })}
          </div>
        </div>
        <div className="chart-grid">
          <article className="panel chart-card"><header><div><span className="panel__eyebrow">Нагрузка</span><h3>Прогнозы по дням</h3></div><span className="legend dashboard-legend--bar"><i />Выдано</span></header><TrendChart data={dailySeries(summary.series_forecasts_per_day)} bars label="Выдано прогнозов по дням" seriesLabel="Выдано" gapLabel="расчёта не было" /></article>
          <article className="panel chart-card"><header><div><span className="panel__eyebrow">Данные</span><h3>Охват расчёта</h3></div><span className="legend legend--blue"><i />Доля каналов</span></header><TrendChart data={dailySeries(summary.series_coverage_per_day)} percent label="Охват расчёта по дням" seriesLabel="Доля каналов" gapLabel="расчёта не было" /></article>
        </div>
      </div>
      <aside className="panel priority-panel"><header><div><span className="panel__eyebrow">Приоритет</span><h3>Очередь диспетчера</h3></div><Link to="/forecasts">Все</Link></header><div className="priority-panel__body"><Loaded load={queue}>{(items) => items.length === 0 ? <StateView state="empty" detail="Открытых прогнозов без решения сейчас нет." /> : <><ol className="priority-list">{items.map((item) => <li key={item.id}><span className="rank" title={`Место в выдаче сценария за ${fmtDate(item.asof)}`}>{item.rank}</span><div><Link to={`/forecasts/${encodeURIComponent(item.id)}`} title={placeText(item)}>{placeText(item)}</Link><small><span className={`scenario-tag scenario-tag--${item.scenario}`} title={item.scenario_title}><span>{shortScenario(item.scenario_title)}</span></span><span className="dashboard-queue__score" title={scoreText(item)}>{scoreShort(item)}</span></small></div><Icon name="arrow" /></li>)}</ol>{items.length < QUEUE_SIZE && <p className="dashboard-queue__all">Других открытых прогнозов без решения нет.</p>}</>}</Loaded></div><div className="queue-footer"><Icon name="activity" /><span>Очередь обновляется после нового расчёта или по кнопке «Обновить»</span></div></aside>
    </div>
    <div className="panel orders-overview"><div><Icon name="orders"/><span><strong>Заявки по статусам</strong><small>Операционная загрузка службы эксплуатации</small></span></div><div>{Object.entries(summary.work_orders_by_status).map(([status,count]) => <Link key={status} to={`/work-orders?status=${encodeURIComponent(status)}`}><strong>{count}</strong><span>{title("work_order_status", status)}</span></Link>)}</div><Link className="orders-overview__all" to="/work-orders">Открыть все <Icon name="arrow"/></Link></div>
  </>;
}

function Metric({ icon, tone, value, label, detail }: { icon: Parameters<typeof Icon>[0]["name"]; tone: string; value: string | number; label: string; detail: string }) {
  return <article className={`metric-card metric-card--${tone}`}><div className="metric-card__icon"><Icon name={icon} /></div><div><strong>{value}</strong><span>{label}</span><small>{detail}</small></div></article>;
}

/** «Износ: плановая диагностика…» → «Износ»: так сценарии названы и в строке статуса, полное имя — в подсказке. */
function shortScenario(value: string): string {
  return value.split(":")[0].trim() || value;
}

function percentText(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)} %`;
}

/**
 * Оценка в строке очереди: в узкой колонке полная подпись типа оценки обрезалась
 * вместе с самим числом. Слово «приоритет» не даёт прочитать 17 как вероятность;
 * полная подпись — во всплывающей подсказке.
 */
function scoreShort(item: Pick<Forecast, "score_type" | "risk" | "priority_score">): string {
  return item.score_type === "probability" ? `вероятность ${fmtNumber(item.risk)}` : `приоритет ${fmtNumber(item.priority_score)}`;
}

function plural(count: number, one: string, few: string, many: string): string {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}
