import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { fmtDateTime } from "../format";
import { KIND_TITLES, useStream, type StreamEvent } from "../stream/useStream";
import { Icon } from "./Icons";

function linkOf(event: StreamEvent): string | null {
  const id = event.payload.id;
  if (event.kind === "alert.new" && typeof id === "string") return `/forecasts/${encodeURIComponent(id)}`;
  if (event.kind === "workorder.changed" && typeof id === "string") return `/work-orders?open=${encodeURIComponent(id)}`;
  if (event.kind === "event.alarm") return "/events";
  return null;
}

export function ToastCenter() {
  const { events } = useStream();
  const [visible, setVisible] = useState<StreamEvent[]>([]);
  const seen = useRef(0);

  useEffect(() => {
    const latest = events[0];
    if (!latest || latest.seq <= seen.current) return;
    seen.current = latest.seq;
    setVisible((items) => [latest, ...items].slice(0, 3));
  }, [events]);

  // Стабильная ссылка: иначе каждое новое событие потока перезапускало таймеры всех тостов.
  const dismiss = useCallback((seq: number) => setVisible((items) => items.filter((item) => item.seq !== seq)), []);

  if (!visible.length) return null;
  return <div className="toast-stack" aria-live="polite">{visible.map((event) => <Toast key={event.seq} event={event} dismiss={dismiss} />)}</div>;
}

function Toast({ event, dismiss }: { event: StreamEvent; dismiss: (seq: number) => void }) {
  const close = () => dismiss(event.seq);
  useEffect(() => { const timer = window.setTimeout(() => dismiss(event.seq), event.severity === "critical" ? 10000 : 6500); return () => window.clearTimeout(timer); }, [dismiss, event.seq, event.severity]);
  const link = linkOf(event);
  // Подпись — вид события: заголовок сервера («Заявка WO-…», «Расчёт за …») сам не говорит, что случилось.
  const body = <><div className="toast__icon"><Icon name={event.kind === "workorder.changed" ? "wrench" : event.kind === "alert.new" ? "forecast" : "bell"} /></div><div><span>{fmtDateTime(event.ts)}</span><strong>{event.title}</strong><small>{event.severity === "critical" ? "Требует немедленного внимания" : KIND_TITLES[event.kind]}</small></div></>;
  return <article className={`toast toast--${event.severity}`}>{link ? <Link to={link} onClick={close}>{body}</Link> : <div className="toast__body">{body}</div>}<button type="button" onClick={close} aria-label="Закрыть">×</button></article>;
}
