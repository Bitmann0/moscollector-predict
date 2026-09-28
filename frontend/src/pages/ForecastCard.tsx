import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { SourceBadge } from "../components/common";
import { LineChart } from "../components/Charts";
import { Icon } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Loaded, StateView } from "../components/StateView";
import { fmtDate, fmtDateTime, fmtNumber, fmtPercent, fmtSigned, placeText } from "../format";
import { ACTIONS, title, type Action } from "../vocab";

type Card = Schemas["ForecastCard"];
type ReasonCode = Schemas["ReasonCodeOut"];
type Outcome = Schemas["OutcomeIn"]["outcome"];

export function ForecastCard() {
  const { id = "" } = useParams();
  const { can } = useAuth();
  const card = useLoad(
    () => api.GET("/api/v1/forecasts/{forecast_id}", { params: { path: { forecast_id: id } } }),
    [id],
  );
  // Номер созданного черновика живёт здесь: после перезагрузки у карточки есть заявка,
  // блок с кнопкой исчезает вместе со своим сообщением, и без этого подтверждения не видно.
  const [draftId, setDraftId] = useState<string | null>(null);

  return (
    <section>
      <Loaded load={card}>
        {(data) => (
          <>
            <CardView card={data} />
            {can("work_order_manage") && !data.work_order_id && (
              <DraftOrderButton forecastId={data.id} onCreated={(orderId) => { setDraftId(orderId); card.reload(); }} />
            )}
            {draftId && data.work_order_id === draftId && (
              <div className="draft-order" role="status"><div><strong>Черновик заявки {draftId} сформирован</strong><span>Заявка связана с этим прогнозом</span></div><Link className="button" to={`/work-orders?open=${encodeURIComponent(draftId)}`}>Открыть заявку</Link></div>
            )}
            <div className="forecast-actions">
              {can("decide") ? (
                <DecisionForm forecastId={data.id} onSaved={card.reload} />
              ) : (
                <p className="muted">Решение по прогнозу принимает диспетчер.</p>
              )}
              {can("outcome") && <OutcomeForm forecastId={data.id} channel={data.channel?.id} current={data.outcome_manual} onSaved={card.reload} />}
            </div>
          </>
        )}
      </Loaded>
    </section>
  );
}

function DraftOrderButton({ forecastId, onCreated }: { forecastId: string; onCreated: (orderId: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  async function create() {
    setBusy(true); setMessage(null);
    try {
      const { data, error, response } = await api.POST("/api/v1/work-orders", { body: { forecast_ids: [forecastId] } });
      if (data) onCreated(data.id);
      else setMessage(errorText(error, response));
    } catch { setMessage(errorText(null, undefined)); }
    finally { setBusy(false); }
  }
  return <div className="draft-order"><div><strong>Превентивное обслуживание</strong><span>Сформировать заявку из факторов и объекта этого прогноза</span></div><button type="button" className="button button--primary" disabled={busy} onClick={() => void create()}>{busy ? "Формирование…" : "Создать черновик заявки"}</button>{message && <p role="alert">{message}</p>}</div>;
}

function CardView({ card }: { card: Card }) {
  const channel = card.channel;
  // Списки с default_factory в pydantic в OpenAPI необязательны: пустой — то же, что нет.
  const factors = card.factors ?? [];
  const decisions = card.decisions ?? [];
  // backend отдаёт пересчёты от старых к новым; показываем, как историю решений, — новые сверху
  const versions = [...(card.versions ?? [])].reverse();
  // Динамика строится по одному каналу. У недельной рекомендации по объекту канала нет,
  // backend отдаёт 30 суток без событий, и график из одних «нет данных» ничего не скажет.
  const dynamics = channel ? card.dynamics_30d ?? [] : [];
  // Сутки без событий канала — «нет данных», а не ноль (events = 0). Если наблюдаемых суток нет
  // или в каждых из них тревог и плохих состояний ноль, график из пустоты не рисуем — пишем словами.
  const observedDays = dynamics.filter((p) => p.events > 0);
  const quiet = observedDays.every((p) => p.alarms === 0 && p.bad_states === 0);
  const maxContribution = Math.max(...factors.map((item) => Math.abs(item.contribution)), 0.01);
  const probability = card.score_type === "probability";
  const score = probability ? fmtPercent(card.risk) : fmtNumber(card.priority_score);
  return (
    <>
      <PageHeader eyebrow="Карточка прогноза" title={card.scenario_title} description={`${placeText(card)} · прогноз от ${fmtDate(card.asof)}`} actions={<Link className="button" to="/forecasts">← К журналу</Link>} />
      {card.data_status !== "ok" && <StateView state={card.data_status} />}
      <div className="forecast-hero panel"><div className="forecast-score"><span>{capital(title("score_type", card.score_type))}</span><strong>{score}</strong><small>{probability ? "события в окне прогноза" : "чем больше, тем выше в очереди"}</small></div><div className="forecast-window"><Icon name="calendar" /><div><span>Окно прогноза · {card.horizon_hours} ч</span><strong>{fmtDateTime(card.valid_from)} — {fmtDateTime(card.valid_to)}</strong><small>{calendarText(card)}</small></div></div><div className="forecast-rank"><span>Место в очереди</span><strong>№ {card.rank}</strong><SourceBadge source={card.source} /></div></div>
      {/* У недельной рекомендации без графика левая колонка короче паспорта в полтора раза —
          паспорт тогда встаёт под неё на всю ширину, а не оставляет пустоту над формами */}
      <div className={`forecast-layout${card.kind === "weekly_recommendation" && dynamics.length === 0 ? " forecast-layout--stacked" : ""}`}><div className="forecast-main">
      {card.kind === "weekly_recommendation" && <div className="weekly-evidence panel"><div><span className="panel__eyebrow">Основание рекомендации</span><h3>{evidenceText(card.evidence)}</h3>{card.coverage_note && <p>{coverageText(card.coverage_note)}</p>}</div><AlarmStat days={card.recent_alarm_days_7} period="за 7 суток" /><AlarmStat days={card.recent_alarm_days_30} period="за 30 суток" /></div>}
      {dynamics.length > 0 && (quiet ? (
        // Пустой график с дробной осью на полэкрана — тот же факт короче словами.
        <article className="panel dynamics-card dynamics-card--quiet"><div><span className="panel__eyebrow">Контекст</span><h3>Активность за 30 суток</h3></div><p>{quietText(dynamics, observedDays.length)}</p></article>
      ) : <article className="panel dynamics-card"><header><div><span className="panel__eyebrow">Контекст</span><h3>Активность за 30 суток</h3></div><div className="dynamics-legend"><span><i/>Тревоги</span><span><i/>Плохие состояния</span></div></header><DynamicsChart points={dynamics} /></article>)}
      {card.kind !== "weekly_recommendation" && <><h2 className="section-title">Почему модель подняла риск</h2>
      {factors.length === 0 ? (
        <p className="muted">Факторы для этого прогноза не переданы.</p>
      ) : (
        <ul className="factors">
          {factors.map((factor) => (
            <li key={factor.feature}>
              <div><span>{capital(factor.label)}</span><small>{factor.contribution >= 0 ? "Повышает риск" : "Снижает риск"}</small></div><div className="factor-track"><i className={factor.contribution >= 0 ? "factor-up" : "factor-down"} style={{ width: `${Math.abs(factor.contribution) / maxContribution * 100}%` }} /></div><strong className={factor.contribution >= 0 ? "up" : "down"}>{fmtSigned(factor.contribution)}</strong>
            </li>
          ))}
        </ul>
      )}</>}

      <div className="forecast-history-grid">{decisions.length > 0 && (
        <article><h2 className="section-title">История решений</h2>
          <ul className="history">
            {decisions.map((decision) => (
              <li key={decision.id}>
                <span className="history__dot"/><div><strong>{title("action", decision.action)}</strong><span>{fmtDateTime(decision.created_at)} · {decision.author}</span><p>{title("reason_code", decision.reason_code)}{decision.comment ? `. ${decision.comment}` : ""}</p></div><SourceBadge source={decision.source} />
              </li>
            ))}
          </ul>
        </article>
      )}

      {versions.length > 0 && (
        <article><h2 className="section-title">Пересчёты модели</h2>
          <table className="table table--narrow">
            <thead>
              <tr>
                <th>Записано</th>
                <th className="num">Расчёт</th>
                {/* У относительного приоритета вероятности нет: колонка была бы из одних прочерков */}
                {probability && <th className="num">Вероятность</th>}
                <th className="num">Место</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={version.run_id}>
                  <td>{fmtDateTime(version.recorded_at)}</td>
                  <td className="num">№ {version.run_id}</td>
                  {probability && <td className="num">{fmtPercent(version.risk)}</td>}
                  <td className="num">{version.rank}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      )}</div></div>
      <aside className="forecast-side panel"><span className="panel__eyebrow">Паспорт риска</span><h3>{card.object.name ?? card.object.id ?? "Объект"}</h3><dl><Field label="Комплекс">{card.object.complex_name ?? card.object.complex_id ?? "—"}</Field>{card.object.kind_ru && <Field label="Тип объекта">{card.object.kind_ru}</Field>}{channel && <Field label="Датчик">{channel.sensor_type ?? channel.name ?? "—"}</Field>}{channel?.name_decoded && <Field label="Расшифровка канала">{capital(channel.name_decoded)}</Field>}<Field label="Вид прогноза">{title("kind", card.kind)}</Field><Field label="Факт по данным">{card.outcome_auto ? title("outcome_auto", card.outcome_auto) : <span className="muted">Ещё не определён</span>}</Field><Field label="Итог проверки">{card.outcome_manual ? title("outcome_manual", card.outcome_manual) : <span className="muted">Не внесён</span>}</Field>{card.coverage_note && <Field label="Охват">{coverageText(card.coverage_note)}</Field>}<Field label="Код случая"><code className="case-key">{card.case_key}</code></Field></dl>{card.work_order_id && <Link className="linked-order" to={`/work-orders?open=${encodeURIComponent(card.work_order_id)}`}><Icon name="wrench"/><span><small>Связанная заявка</small><strong>{card.work_order_id}</strong></span><Icon name="arrow"/></Link>}</aside></div>
    </>
  );
}

function DynamicsChart({ points }: { points: NonNullable<Card["dynamics_30d"]> }) {
  // Сутки без событий канала — «нет данных», а не ноль тревог: у потери связи
  // молчание канала и есть симптом, нулевая линия выдала бы его за исправность.
  const observed = points.filter((p) => p.events > 0).length;
  const missing = points.length - observed;
  const known = (value: number, events: number) => (events > 0 ? value : null);
  const note = observed === 0
    ? `За ${points.length} суток событий канала в журнале сервиса нет. Отсутствие событий не доказывает исправность.`
    : missing === 0
      ? `События канала есть в журнале сервиса за все ${points.length} суток.`
      : `Дней с событиями канала в журнале сервиса: ${observed} из ${points.length}, без событий: ${missing}. Дни без событий на графике — разрывы, а не ноль: отсутствие событий не доказывает исправность.`;
  return (
    <>
      <LineChart days={points.map((p) => p.day)} label="Тревоги и плохие состояния за 30 суток" series={[
        { key: "alarms", label: "Тревоги", color: "var(--alarm)", values: points.map((p) => known(p.alarms, p.events)) },
        { key: "bad", label: "Плохие состояния", color: "var(--data)", values: points.map((p) => known(p.bad_states, p.events)), dashed: true },
      ]} />
      <p className="chart-note">{note}</p>
    </>
  );
}

/** Строка вместо графика: либо канал молчал все сутки, либо в наблюдаемые сутки не было ни тревог, ни плохих состояний. */
function quietText(points: NonNullable<Card["dynamics_30d"]>, observed: number): string {
  const period = `${fmtDate(points[0].day)} — ${fmtDate(points[points.length - 1].day)}`;
  if (observed === 0) return `За ${points.length} суток событий канала в журнале сервиса нет · ${period}. Отсутствие событий не доказывает исправность.`;
  const missing = points.length - observed;
  const tail = missing === 0 ? "" : `. Суток без событий канала: ${missing} из ${points.length} — по ним состояние неизвестно`;
  return `Тревог и плохих состояний по датчику не было · ${period}${tail}`;
}

function AlarmStat({ days, period }: { days: number | null | undefined; period: string }) {
  return <div className="alarm-stat"><strong>{days ?? "—"}</strong><span>{plural(days ?? 0, "день", "дня", "дней")} с тревогами<br/>{period}</span></div>;
}

/** Подписи словаря и признаков приходят со строчной, а в карточке стоят как заголовки. */
function capital(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

/** «1 день», «4 дня», «5 дней», «11 дней», «21 день». */
function plural(n: number, one: string, few: string, many: string): string {
  const d10 = n % 10, d100 = n % 100;
  if (d10 === 1 && d100 !== 11) return one;
  if (d10 >= 2 && d10 <= 4 && (d100 < 12 || d100 > 14)) return few;
  return many;
}

/** Охват backend собирает строкой без разрядов: «2666 из 11485 (23%)» → «2 666 из 11 485 (23 %)». */
function coverageText(note: string): string {
  return note.replace(/\d{4,}/g, (digits) => fmtNumber(Number(digits))).replace(/(\d)%/g, "$1\u00a0%");
}

/** День недели backend даёт по началу окна; у недельного окна одно слово «среда» читается как «только в среду». */
function calendarText(card: Card): string {
  if (!card.calendar) return "Календарный контекст не передан";
  const day = card.horizon_hours > 24 ? `Первый день — ${card.calendar.weekday_title}` : capital(card.calendar.weekday_title);
  return card.calendar.holiday ? `${day} · ${card.calendar.holiday}` : day;
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
      {/* Шапка того же вида, что у соседней формы «Результат проверки» */}
      <div className="decision__head">
        <span className="panel__eyebrow">Действие диспетчера</span>
        <h2>Решение</h2>
        <p>Сохраняется в истории карточки и в журнале прогнозов.</p>
      </div>
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

// Подписи — из vocabularies.json (C3), здесь только подсказки к ним.
const OUTCOMES: { code: Outcome; hint: string }[] = [
  { code: "confirmed_event", hint: "Риск реализовался" },
  { code: "sensor_fault", hint: "Проблема в средстве контроля" },
  { code: "normal_activation", hint: "Оборудование исправно" },
  { code: "no_event", hint: "Прогноз не подтвердился" },
  { code: "unknown", hint: "Недостаточно данных" },
];

function OutcomeForm({ forecastId, channel, current, onSaved }: { forecastId: string; channel?: number; current?: Outcome | null; onSaved: () => void }) {
  const [outcome, setOutcome] = useState<Outcome | "">(current ?? "");
  const [eventAt, setEventAt] = useState("");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!outcome) return; setBusy(true); setMessage(null);
    try {
      const { data, error, response } = await api.POST("/api/v1/forecasts/{forecast_id}/outcome", { params: { path: { forecast_id: forecastId } }, body: { outcome, channel: channel ?? null, event_at: eventAt ? new Date(eventAt).toISOString() : null, comment: comment.trim() || null } });
      if (data) { setMessage({ ok: true, text: "Результат проверки сохранён" }); onSaved(); }
      else setMessage({ ok: false, text: errorText(error, response) });
    } catch { setMessage({ ok: false, text: errorText(null, undefined) }); }
    finally { setBusy(false); }
  }
  return <form className="outcome-form panel" onSubmit={(event) => void submit(event)}><div className="outcome-form__head"><div><span className="panel__eyebrow">Контур обратной связи</span><h2>Результат проверки</h2><p>Итог проверки сохраняется в карточке и в журнале прогнозов.</p></div><Icon name="shield" /></div><div className="outcome-options">{OUTCOMES.map((item) => <label key={item.code} className={outcome === item.code ? "active" : ""}><input type="radio" name="outcome" value={item.code} checked={outcome === item.code} onChange={() => setOutcome(item.code)} /><span><strong>{title("outcome_manual", item.code)}</strong><small>{item.hint}</small></span></label>)}</div><div className="outcome-fields"><label className="field"><span>Время события, если известно</span><input type="datetime-local" value={eventAt} onChange={(event) => setEventAt(event.target.value)} /></label><label className="field"><span>Комментарий специалиста</span><input value={comment} onChange={(event) => setComment(event.target.value)} maxLength={2000} placeholder="Что обнаружено при проверке" /></label><button className="button button--primary" disabled={busy || !outcome}>{busy ? "Сохранение…" : "Сохранить результат"}</button></div>{message && <p className={message.ok ? "form-ok" : "form-error"} role="status">{message.text}</p>}</form>;
}

/** Основание недельной рекомендации — код правила из guard_weekly.py словами. */
const EVIDENCE: Record<string, string> = {
  alarm_on_at_least_4_of_previous_7_days: "Охранная тревога не меньше чем в 4 из 7 предыдущих дней",
};

function evidenceText(code: string | null | undefined): string {
  if (!code) return "Повторяющиеся охранные тревоги";
  return EVIDENCE[code] ?? code;
}
