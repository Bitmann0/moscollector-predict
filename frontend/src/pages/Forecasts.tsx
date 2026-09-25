/**
 * ЗАГЛУШКА — владелец FE-03 (C2). Заменить: таблицу на журнал прогнозов по ТЗ §10 —
 * фильтры по дате, решению, факту и объекту, группировку по объекту и case_key,
 * строку итога по неделе (выдано / попало / неизвестно).
 * Контракт: типы из src/api/schema.d.ts (Page_ForecastItem_, GET /api/v1/forecasts);
 * npm run typecheck должен остаться зелёным.
 */
import { Link, useSearchParams } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Pager, SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtDateTime, pageParam, placeText, scoreText } from "../format";
import { SCENARIOS, title, type Scenario } from "../vocab";

const PAGE_SIZE = 50;

type Forecast = Schemas["ForecastItem"];

function scenarioParam(value: string | null): Scenario | undefined {
  return SCENARIOS.find((s) => s.code === value)?.code;
}

export function Forecasts() {
  const [params, setParams] = useSearchParams();
  const scenario = scenarioParam(params.get("scenario"));
  const page = pageParam(params.get("page"));

  const load = useLoad(
    () =>
      api.GET("/api/v1/forecasts", {
        params: { query: { scenario, page, page_size: PAGE_SIZE } },
      }),
    [scenario, page],
  );

  function update(next: { scenario?: Scenario; page?: number }) {
    const query = new URLSearchParams();
    if (next.scenario) query.set("scenario", next.scenario);
    if (next.page && next.page > 1) query.set("page", String(next.page));
    setParams(query);
  }

  return (
    <section>
      <h1>Прогнозы</h1>
      <div className="toolbar">
        <label className="field field--inline">
          <span>Сценарий</span>
          <select
            value={scenario ?? ""}
            onChange={(event) => update({ scenario: scenarioParam(event.target.value) })}
          >
            <option value="">Все сценарии</option>
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
            {data.items.length === 0 ? (
              <StateView state="empty" />
            ) : (
              <ForecastTable items={data.items} />
            )}
            <Pager
              page={data.page}
              pageSize={data.page_size}
              total={data.total}
              onPage={(p) => update({ scenario, page: p })}
            />
          </>
        )}
      </Loaded>
    </section>
  );
}

function ForecastTable({ items }: { items: Forecast[] }) {
  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th className="num">№</th>
            <th>Дата прогноза</th>
            <th>Окно</th>
            <th>Сценарий</th>
            <th>Объект · пикет · канал</th>
            <th>Оценка</th>
            <th>Решение</th>
            <th>Факт по данным</th>
            <th>Итог проверки</th>
            <th>Заявка</th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <tr key={item.id}>
              <td className="num">{item.rank}</td>
              <td>{fmtDate(item.asof)}</td>
              <td className="nowrap">
                {fmtDateTime(item.valid_from)} — {fmtDateTime(item.valid_to)}
              </td>
              <td>{item.scenario_title}</td>
              <td>
                <Link to={`/forecasts/${encodeURIComponent(item.id)}`}>{placeText(item)}</Link>{" "}
                <SourceBadge source={item.source} />
              </td>
              <td>{scoreText(item)}</td>
              <td>{item.decision ? title("action", item.decision.action) : "—"}</td>
              <td>{title("outcome_auto", item.outcome_auto)}</td>
              <td>{title("outcome_manual", item.outcome_manual)}</td>
              <td>{item.work_order_id ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
