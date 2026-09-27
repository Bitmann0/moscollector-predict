import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Loaded, StateView, headState } from "../components/StateView";
import { fmtDate, fmtDateTime } from "../format";
import { useReloadOn } from "../stream/useStream";
import { SCENARIOS, SCENARIO_SHORT, title, vocab } from "../vocab";

export function ScenarioStatus() {
  const { scenario: code } = useParams();
  const scenario = vocab.scenario.find((item) => item.code === code);
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  useReloadOn(["run.finished"], status.reload);

  if (!scenario) return <StateView state="empty" detail="Неизвестный сценарий расчёта." />;

  return <section>
    <PageHeader eyebrow="Состояние расчёта" title={scenario.title} description="Причина выдачи или отсутствия прогноза по выбранному направлению" actions={<button className="button" type="button" onClick={status.reload}>Обновить статус</button>} />
    <nav className="scenario-switch" aria-label="Сценарии расчёта">
      {SCENARIOS.map((item) => <Link key={item.code} to={`/scenarios/${item.code}`} aria-current={item.code === code ? "page" : undefined}>{SCENARIO_SHORT[item.code] ?? item.title}</Link>)}
    </nav>
    <Loaded load={status}>{(data) => {
      const head = data.heads.find((item) => item.scenario === code);
      const state = head ? headState(head) ?? "not_run" : "not_run";
      return <>
        <div className="scenario-status-grid">
          <article className={`panel scenario-status-main scenario-status-main--${scenario.code}`}>
            <span className="panel__eyebrow">Последний результат · {scenario.head}</span>
            <StateView state={state} detail={head?.detail} />
            {state === "ok" && <p className="scenario-status-note">Расчёт завершён. Выданные прогнозы и решения диспетчера доступны в журнале.</p>}
            {data.ml.mode === "stub" && <p className="filter-notice">Сейчас подключена заглушка ML: результат демонстрационный, не эксплуатационный.</p>}
          </article>
          <aside className="panel scenario-status-details">
            <h2>Контекст</h2>
            <dl>
              <div><dt>Горизонт</dt><dd>{scenario.horizon_hours} ч</dd></div>
              <div><dt>Тип оценки</dt><dd>{title("score_type", scenario.score_type)}</dd></div>
              <div><dt>Демо-день</dt><dd>{fmtDate(data.demo_today)}</dd></div>
              <div><dt>Данные ML по</dt><dd>{data.ml.data_last_day ? fmtDate(data.ml.data_last_day) : "неизвестно"}</dd></div>
              <div><dt>Задержка модели</dt><dd>{head?.model_lag_days == null ? "неизвестно" : `${head.model_lag_days} сут.`}</dd></div>
              <div><dt>Порог</dt><dd>{head?.threshold_feasible === false ? "Неосуществим — сценарий молчит" : head?.threshold_feasible === true ? "Осуществим" : "Нет сведений"}</dd></div>
              <div><dt>Сервис ML</dt><dd>{data.ml.reachable ? "На связи" : "Недоступен"}</dd></div>
            </dl>
          </aside>
        </div>
        {data.last_run && <p className="scenario-status-run">Последний общий прогон системы: {fmtDate(data.last_run.asof)}{data.last_run.finished_at ? ` · завершён ${fmtDateTime(data.last_run.finished_at)}` : ""}. Его дата не обязательно относится к этому сценарию.</p>}
        <Link className="button scenario-status-action" to={`/forecasts?scenario=${encodeURIComponent(scenario.code)}`}>Открыть прогнозы сценария →</Link>
      </>;
    }}</Loaded>
  </section>;
}
