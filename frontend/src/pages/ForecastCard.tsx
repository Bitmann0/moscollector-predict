/**
 * ЗАГЛУШКА — владелец FE-04 (C2). Заменить: факторы полосами без процентов,
 * динамику за 30 суток, календарный контекст, для недельной очереди — evidence и
 * счётчики за 7 и 30 дней крупно, историю решений, кнопку «Черновик заявки».
 * Форма решения уже живая: действие, причина из /reason-codes по допустимым
 * действиям, комментарий.
 * Контракт: типы из src/api/schema.d.ts (ForecastCard, DecisionIn, ReasonCodeOut);
 * npm run typecheck должен остаться зелёным.
 */
import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { SourceBadge } from "../components/common";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtDateTime, fmtNumber, fmtSigned, placeText, scoreText } from "../format";
import { ACTIONS, title, type Action } from "../vocab";

type Card = Schemas["ForecastCard"];
type ReasonCode = Schemas["ReasonCodeOut"];

export function ForecastCard() {
  const { id = "" } = useParams();
  const { can } = useAuth();
  const card = useLoad(
    () => api.GET("/api/v1/forecasts/{forecast_id}", { params: { path: { forecast_id: id } } }),
    [id],
  );

  return (
    <section>
      <p>
        <Link to="/forecasts">← К списку прогнозов</Link>
      </p>
      <Loaded load={card}>
        {(data) => (
          <>
            <CardView card={data} />
            {can("work_order_manage") && !data.work_order_id && (
              <DraftOrderButton forecastId={data.id} onCreated={card.reload} />
            )}
            {can("decide") ? (
              <DecisionForm forecastId={data.id} onSaved={card.reload} />
            ) : (
              <p className="muted">Решение по прогнозу принимает диспетчер.</p>
            )}
          </>
        )}
      </Loaded>
    </section>
  );
}

function DraftOrderButton({ forecastId, onCreated }: { forecastId: string; onCreated: () => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  async function create() {
    setBusy(true); setMessage(null);
    try {
      const { data, error, response } = await api.POST("/api/v1/work-orders", { body: { forecast_ids: [forecastId] } });
      if (data) { setMessage(`Черновик ${data.id} сформирован`); onCreated(); }
      else setMessage(errorText(error, response));
    } catch { setMessage(errorText(null, undefined)); }
    finally { setBusy(false); }
  }
  return <div className="draft-order"><div><strong>Превентивное обслуживание</strong><span>Сформировать заявку из факторов и объекта этого прогноза</span></div><button type="button" className="button button--primary" disabled={busy} onClick={() => void create()}>{busy ? "Формирование…" : "Создать черновик заявки"}</button>{message && <p role="status">{message}</p>}</div>;
}

function CardView({ card }: { card: Card }) {
  const channel = card.channel;
  // Списки с default_factory в pydantic в OpenAPI необязательны: пустой — то же, что нет.
  const factors = card.factors ?? [];
  const decisions = card.decisions ?? [];
  const versions = card.versions ?? [];
  return (
    <>
      <h1>
        {card.scenario_title} <SourceBadge source={card.source} />
      </h1>
      {card.data_status !== "ok" && <StateView state={card.data_status} />}
      <dl className="fields">
        <Field label="Вид">{title("kind", card.kind)}</Field>
        <Field label="Дата прогноза">{fmtDate(card.asof)}</Field>
        <Field label="Окно">
          {fmtDateTime(card.valid_from)} — {fmtDateTime(card.valid_to)} ({card.horizon_hours} ч)
        </Field>
        <Field label="Оценка">{scoreText(card)}</Field>
        <Field label="Место в очереди">{card.rank}</Field>
        <Field label="Объект">{placeText(card)}</Field>
        <Field label="Комплекс">{card.object.complex_name ?? card.object.complex_id ?? "—"}</Field>
        <Field label="Тип объекта">{card.object.kind_ru ?? "—"}</Field>
        {channel && <Field label="Тип датчика">{channel.sensor_type ?? "—"}</Field>}
        <Field label="Факт по данным">{title("outcome_auto", card.outcome_auto)}</Field>
        <Field label="Итог проверки">{title("outcome_manual", card.outcome_manual)}</Field>
        <Field label="Заявка">{card.work_order_id ?? "—"}</Field>
        {card.evidence && <Field label="Основание">{card.evidence}</Field>}
        {card.recent_alarm_days_7 != null && (
          <Field label="Дней с тревогами за 7 / 30 суток">
            {card.recent_alarm_days_7} / {card.recent_alarm_days_30 ?? "—"}
          </Field>
        )}
        {card.coverage_note && <Field label="Охват">{card.coverage_note}</Field>}
        {card.calendar && (
          <Field label="Календарь">
            {card.calendar.weekday_title}
            {card.calendar.holiday ? `, ${card.calendar.holiday}` : ""}
          </Field>
        )}
      </dl>

      <h2>Факторы</h2>
      {factors.length === 0 ? (
        <p className="muted">Факторы для этого прогноза не переданы.</p>
      ) : (
        <ul className="factors">
          {factors.map((factor) => (
            <li key={factor.feature}>
              <span>{factor.label}</span>{" "}
              <span className={factor.contribution >= 0 ? "up" : "down"}>
                {fmtSigned(factor.contribution)} {factor.contribution >= 0 ? "повышает риск" : "снижает риск"}
              </span>
            </li>
          ))}
        </ul>
      )}

      {decisions.length > 0 && (
        <>
          <h2>Решения</h2>
          <ul className="history">
            {decisions.map((decision) => (
              <li key={decision.id}>
                {fmtDateTime(decision.created_at)} — {title("action", decision.action)}:{" "}
                {title("reason_code", decision.reason_code)} ({decision.author})
                {decision.comment ? `. ${decision.comment}` : ""} <SourceBadge source={decision.source} />
              </li>
            ))}
          </ul>
        </>
      )}

      {versions.length > 0 && (
        <>
          <h2>Пересчёты</h2>
          <table className="table table--narrow">
            <thead>
              <tr>
                <th>Записано</th>
                <th className="num">Расчёт</th>
                <th className="num">Риск</th>
                <th className="num">Место</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={version.run_id}>
                  <td>{fmtDateTime(version.recorded_at)}</td>
                  <td className="num">{version.run_id}</td>
                  <td className="num">{fmtNumber(version.risk)}</td>
                  <td className="num">{version.rank}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="fields__row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function DecisionForm({ forecastId, onSaved }: { forecastId: string; onSaved: () => void }) {
  const reasons = useLoad(() => api.GET("/api/v1/reason-codes"), []);
  const [action, setAction] = useState<Action | "">("");
  const [reason, setReason] = useState<ReasonCode["code"] | "">("");
  const [comment, setComment] = useState("");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const allowed: ReasonCode[] =
    action && reasons.data ? reasons.data.filter((r) => r.actions.includes(action)) : [];

  function chooseAction(next: Action | "") {
    setAction(next);
    // Причина из прежнего действия к новому может не подходить.
    if (!reasons.data?.some((r) => r.code === reason && next !== "" && r.actions.includes(next))) {
      setReason("");
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!action || !reason) return;
    setBusy(true);
    setMessage(null);
    try {
      const { data, error, response } = await api.POST("/api/v1/forecasts/{forecast_id}/decisions", {
        params: { path: { forecast_id: forecastId } },
        body: { action, reason_code: reason, comment: comment.trim() || null },
      });
      if (data) {
        setMessage({ ok: true, text: "Решение сохранено" });
        setAction("");
        setReason("");
        setComment("");
        onSaved();
      } else {
        setMessage({ ok: false, text: errorText(error, response) });
      }
    } catch {
      setMessage({ ok: false, text: errorText(null, undefined) });
    } finally {
      setBusy(false);
    }
  }

  if (reasons.status === "error" && !reasons.data) {
    return <StateView state="unavailable" detail={reasons.message} onRetry={reasons.reload} />;
  }

  return (
    <form className="decision" onSubmit={(event) => void submit(event)}>
      <h2>Решение</h2>
      <label className="field">
        <span>Действие</span>
        <select value={action} onChange={(event) => chooseAction(toAction(event.target.value))} required>
          <option value="">Выберите действие</option>
          {ACTIONS.map((a) => (
            <option key={a.code} value={a.code}>
              {a.title}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Причина</span>
        <select
          value={reason}
          onChange={(event) => setReason(allowed.find((r) => r.code === event.target.value)?.code ?? "")}
          disabled={!action || reasons.status === "loading"}
          required
        >
          <option value="">{action ? "Выберите причину" : "Сначала выберите действие"}</option>
          {allowed.map((r) => (
            <option key={r.code} value={r.code}>
              {r.title}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Комментарий</span>
        <textarea
          value={comment}
          onChange={(event) => setComment(event.target.value)}
          maxLength={2000}
          rows={3}
        />
      </label>
      {message && (
        <p className={message.ok ? "form-ok" : "form-error"} role="status">
          {message.text}
        </p>
      )}
      <button type="submit" className="button button--primary" disabled={busy || !action || !reason}>
        {busy ? "Сохранение…" : "Сохранить решение"}
      </button>
    </form>
  );
}

function toAction(value: string): Action | "" {
  return ACTIONS.find((a) => a.code === value)?.code ?? "";
}
