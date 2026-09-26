import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { useLoad } from "../api/useLoad";
import { TrendChart } from "../components/Charts";
import { PageHeader } from "../components/PageHeader";
import { SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtPercent } from "../format";
import { SCENARIOS, type Scenario } from "../vocab";

const DEFAULT_SCENARIO: Scenario = "sensor_link";

function deltaText(rule: number | null | undefined, base: number | null | undefined): string {
  if (rule == null || base == null) return "—";
  const delta = Math.round((rule - base) * 100);
  return `${delta > 0 ? "+" : delta < 0 ? "−" : ""}${Math.abs(delta)} п.п.`;
}

export function Quality() {
  const [params, setParams] = useSearchParams();
  const scenario = SCENARIOS.find((s) => s.code === params.get("scenario"))?.code ?? DEFAULT_SCENARIO;
  const load = useLoad(() => api.GET("/api/v1/quality", { params: { query: { scenario } } }), [scenario]);
  return <section>
    <PageHeader eyebrow="ML monitoring" title="Качество прогноза" description="Прозрачный контроль точности модели на отложенных фактах" actions={<label className="field quality-select"><span>Сценарий</span><select value={scenario} onChange={(e) => setParams({ scenario: e.target.value })}>{SCENARIOS.map((s) => <option key={s.code} value={s.code}>{s.title}</option>)}</select></label>} />
    <Loaded load={load}>{(data) => <>
      <div className="quality-hero"><article className="quality-score"><span>Точность правила</span><strong>{fmtPercent(data.rule_precision)}</strong><small>Precision по проверенным исходам</small></article><article className="quality-score"><span>Базовая частота</span><strong>{fmtPercent(data.base_rate)}</strong><small>Уровень случайного выбора</small></article><article className={`quality-score ${data.rule_precision != null && data.base_rate != null && data.rule_precision < data.base_rate ? "quality-score--decline" : "quality-score--gain"}`}><span>{data.rule_precision != null && data.base_rate != null && data.rule_precision < data.base_rate ? "Снижение к базе" : "Прирост к базе"}</span><strong>{deltaText(data.rule_precision, data.base_rate)}</strong><small>Эффект предиктивной модели</small></article></div>
      <div className="quality-layout"><article className="panel chart-card quality-chart"><header><div><span className="panel__eyebrow">Динамика</span><h3>Precision по неделям</h3></div><SourceBadge source={data.source} /></header>{data.weeks.some((week) => week.precision != null) ? <><TrendChart percent limit={data.base_rate ?? undefined} data={data.weeks.filter((week): week is typeof week & { precision: number } => week.precision != null).map((week) => ({ day: week.week_start, value: week.precision }))} />{data.weeks.some((week) => week.precision == null) && <p className="chart-note">Недели без проверенных исходов исключены из линии и не считаются нулём.</p>}</> : <StateView state="empty" detail="Пока нет проверенных исходов для расчёта Precision." />}</article>
      <article className="panel outcomes-panel"><header><div><span className="panel__eyebrow">Проверка</span><h3>Исходы прогнозов</h3></div></header><div className="outcomes-list">{data.weeks.map((week) => { const total = Math.max(week.issued, 1); return <div key={week.week_start}><div><strong>Неделя {fmtDate(week.week_start)}</strong><span>{week.issued} прогнозов</span></div><div className="stacked" title={`Попадания ${week.hit}, промахи ${week.miss}, неизвестно ${week.unknown}`}><i className="stacked__hit" style={{ width: `${week.hit / total * 100}%` }} /><i className="stacked__miss" style={{ width: `${week.miss / total * 100}%` }} /><i className="stacked__unknown" style={{ width: `${week.unknown / total * 100}%` }} /></div><small><b>{week.hit}</b> попаданий · {week.miss} промахов · {week.unknown} неизвестно</small></div>; })}</div></article></div>
      {data.note && <div className="method-note"><strong>Как читать метрику</strong><p>{data.note}</p></div>}
    </>}</Loaded>
  </section>;
}
