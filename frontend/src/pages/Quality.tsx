/**
 * ЗАГЛУШКА — владелец FE-09 (C2). Заменить: таблицу на экран «Качество прогноза» —
 * точность по неделям с линией базовой частоты, «неизвестно» отдельным цветом,
 * правило отбора, ссылку на методику.
 * Контракт: типы из src/api/schema.d.ts (QualityOut, GET /api/v1/quality);
 * npm run typecheck должен остаться зелёным.
 */
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { useLoad } from "../api/useLoad";
import { SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtPercent } from "../format";
import { SCENARIOS, type Scenario } from "../vocab";

const DEFAULT_SCENARIO: Scenario = "sensor_link";

export function Quality() {
  const [params, setParams] = useSearchParams();
  const scenario = SCENARIOS.find((s) => s.code === params.get("scenario"))?.code ?? DEFAULT_SCENARIO;
  const load = useLoad(
    () => api.GET("/api/v1/quality", { params: { query: { scenario } } }),
    [scenario],
  );

  return (
    <section>
      <h1>Качество прогноза</h1>
      <div className="toolbar">
        <label className="field field--inline">
          <span>Сценарий</span>
          <select value={scenario} onChange={(event) => setParams({ scenario: event.target.value })}>
            {SCENARIOS.map((s) => (
              <option key={s.code} value={s.code}>
                {s.title}
              </option>
            ))}
          </select>
        </label>
      </div>
      <Loaded load={load}>
        {(data) => (
          <>
            <dl className="kpi">
              <div className="kpi__row">
                <dt>Базовая частота события</dt>
                <dd>{fmtPercent(data.base_rate)}</dd>
              </div>
              <div className="kpi__row">
                <dt>Точность правила для сравнения</dt>
                <dd>{fmtPercent(data.rule_precision)}</dd>
              </div>
            </dl>
            {data.note && <p className="muted">{data.note}</p>}
            <p>
              <SourceBadge source={data.source} />
            </p>
            {data.weeks.length === 0 ? (
              <StateView state="empty" />
            ) : (
              <table className="table table--narrow">
                <thead>
                  <tr>
                    <th>Неделя с</th>
                    <th className="num">Выдано</th>
                    <th className="num">Попало</th>
                    <th className="num">Не попало</th>
                    <th className="num">Неизвестно</th>
                    <th className="num">Точность</th>
                  </tr>
                </thead>
                <tbody>
                  {data.weeks.map((week) => (
                    <tr key={week.week_start}>
                      <td>{fmtDate(week.week_start)}</td>
                      <td className="num">{week.issued}</td>
                      <td className="num">{week.hit}</td>
                      <td className="num">{week.miss}</td>
                      <td className="num">{week.unknown}</td>
                      <td className="num">{fmtPercent(week.precision)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </Loaded>
    </section>
  );
}
