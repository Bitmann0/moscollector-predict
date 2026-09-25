/**
 * ЗАГЛУШКА — владелец FE-05 (C2). Заменить: фильтры по статусу, приоритету и
 * сценарию, карточку заявки (GET /api/v1/work-orders/{order_id}), переходы по
 * work_order_transitions из vocabularies.json с причиной и expected_status,
 * историю; на 409 — «Заявку изменил другой пользователь, обновите».
 * Контракт: типы из src/api/schema.d.ts (Page_WorkOrderItem_, WorkOrderCard,
 * WorkOrderTransition); npm run typecheck должен остаться зелёным.
 */
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Pager, SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDateTime, pageParam } from "../format";
import { title } from "../vocab";

const PAGE_SIZE = 50;

export function WorkOrders() {
  const [params, setParams] = useSearchParams();
  const page = pageParam(params.get("page"));
  const load = useLoad(
    () => api.GET("/api/v1/work-orders", { params: { query: { page, page_size: PAGE_SIZE } } }),
    [page],
  );

  return (
    <section>
      <h1>Заявки</h1>
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
                      <th>Номер</th>
                      <th>Статус</th>
                      <th>Приоритет</th>
                      <th>Срок</th>
                      <th>Объект</th>
                      <th>Вид работ</th>
                      <th>Сценарий</th>
                      <th className="num">Прогнозов</th>
                      <th>Создал</th>
                      <th>Создана</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.items.map((order) => (
                      <tr key={order.id}>
                        <td>
                          {order.id} <SourceBadge source={order.source} />
                        </td>
                        <td>{title("work_order_status", order.status)}</td>
                        <td>{title("work_order_priority", order.priority)}</td>
                        <td className="nowrap">{fmtDateTime(order.due_by)}</td>
                        <td>{order.object.name ?? order.object.id ?? "—"}</td>
                        <td>{order.work_type}</td>
                        <td>{title("scenario", order.scenario)}</td>
                        <td className="num">{order.forecast_ids.length}</td>
                        <td>{order.created_by}</td>
                        <td className="nowrap">{fmtDateTime(order.created_at)}</td>
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
