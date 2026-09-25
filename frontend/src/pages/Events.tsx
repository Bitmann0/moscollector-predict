/**
 * ЗАГЛУШКА — владелец FE-06 (C2). Заменить: цвет строки по event_class, скрытие
 * газа в норме по умолчанию, метку «вероятно, плановая проверка» (P0); фильтры и
 * сортировку по каждой колонке и диапазон дат, как в Приложении 2 ТЗ (P1).
 * Автообновление уже живое: при alert.new и event.alarm из SSE страница перечитывается.
 * Контракт: типы из src/api/schema.d.ts (Page_EventItem_, GET /api/v1/events);
 * npm run typecheck должен остаться зелёным.
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Pager } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime, pageParam } from "../format";
import { useReloadOn } from "../stream/useStream";

const PAGE_SIZE = 100;

export function Events() {
  const [params, setParams] = useSearchParams();
  const page = pageParam(params.get("page"));
  const [auto, setAuto] = useState(true);
  const load = useLoad(
    () => api.GET("/api/v1/events", { params: { query: { page, page_size: PAGE_SIZE } } }),
    [page],
  );
  useReloadOn(["alert.new", "event.alarm"], load.reload, { enabled: auto });

  return (
    <section>
      <h1>Журнал событий</h1>
      <div className="toolbar">
        <label className="check">
          <input type="checkbox" checked={auto} onChange={(event) => setAuto(event.target.checked)} />
          Автообновление
        </label>
      </div>
      <Loaded load={load}>
        {(data) => (
          <>
            {data.items.length === 0 ? (
              <StateView state="empty" />
            ) : (
              <div className="table-wrap">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Время регистрации</th>
                      <th>Объект</th>
                      <th>Тип датчика</th>
                      <th>Событие датчика</th>
                      <th>Тип события</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((event) => (
                      <tr key={event.id} className={`event event--${event.event_class}`}>
                        <td className="nowrap">{fmtDateTime(event.ts)}</td>
                        <td>{event.object.name ?? event.object.id ?? "—"}</td>
                        <td>{event.channel.sensor_type ?? "—"}</td>
                        <td>{event.sensor_event ?? "—"}</td>
                        <td>
                          {event.event_class_title}
                          {event.hint && <span className="muted">, {event.hint}</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <Pager
              page={data.page}
              pageSize={data.page_size}
              total={data.total}
              onPage={(p) => setParams(p > 1 ? { page: String(p) } : {})}
            />
          </>
        )}
      </Loaded>
    </section>
  );
}
