/**
 * ЗАГЛУШКА — владелец FE-08 (C2). Заменить: тосты по SSE, ленту из
 * GET /api/v1/notifications (когда BE-08 начнёт хранить уведомления) с отметкой
 * «прочитано» через POST /api/v1/notifications/{notification_id}/read.
 * Сейчас лента — события SSE текущей сессии из useStream, до перезагрузки вкладки.
 * Контракт: типы из src/api/schema.d.ts (NotificationItem) и StreamEvent из
 * src/stream/useStream.ts; npm run typecheck должен остаться зелёным.
 */
import { useEffect } from "react";
import { Link } from "react-router-dom";

import { StateView } from "../components/StateView";
import { fmtDateTime } from "../format";
import {
  KIND_TITLES,
  useStream,
  type Connection,
  type Severity,
  type StreamEvent,
} from "../stream/useStream";

const CONNECTION_TEXT: Record<Connection, string> = {
  idle: "не подключено",
  connecting: "подключается",
  open: "на связи",
  reconnecting: "связь потеряна, переподключаемся",
};

const SEVERITY: Record<Severity, { text: string; tone: string } | null> = {
  info: null,
  warning: { text: "важно", tone: "warn" },
  critical: { text: "срочно", tone: "bad" },
};

/** alert.new несёт id прогноза (backend/app/services/daily_run.py): ведём в карточку. */
function forecastLink(event: StreamEvent): string | null {
  const id = event.payload.id;
  return event.kind === "alert.new" && typeof id === "string" ? `/forecasts/${encodeURIComponent(id)}` : null;
}

export function Notifications() {
  const { events, unread, connection, markAllRead } = useStream();

  // Лента открыта — всё, что в ней видно, прочитано.
  useEffect(() => {
    if (unread > 0) markAllRead();
  }, [unread, markAllRead]);

  return (
    <section>
      <h1>Уведомления</h1>
      <p className="muted">
        Поток событий: {CONNECTION_TEXT[connection]}. Здесь последние 50 событий с момента входа; после
        перезагрузки страницы лента начинается заново.
      </p>
      {events.length === 0 ? (
        <StateView state="empty" />
      ) : (
        <ul className="feed">
          {events.map((event) => {
            const severity = SEVERITY[event.severity];
            const link = forecastLink(event);
            return (
              <li key={event.seq} className={`feed__item feed__item--${event.severity}`}>
                <span className="feed__time">{fmtDateTime(event.ts)}</span>
                <span className="feed__kind">{KIND_TITLES[event.kind]}</span>
                {severity && <span className={`chip chip--${severity.tone}`}>{severity.text}</span>}
                {link ? <Link to={link}>{event.title}</Link> : <span>{event.title}</span>}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
