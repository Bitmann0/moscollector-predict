/**
 * Настраиваемые параметры продукта (ТЗ §18, ML2-13). Живое.
 *
 * Экран администратора: пороги метана, окно «похоже на плановые» для газа, серии ППР/ТО,
 * какие классы и группы аварий уведомляют, лимит рекомендаций по сценарию. Диапазоны
 * полей и проверенные значения приходят с сервера (GET /api/v1/settings/parameters),
 * проверяет тоже сервер: неверное поле — 422, текст ошибки встаёт под полем. На стенде
 * (locked) значения только читаются. Пересчёт принятых событий — POST
 * /api/v1/admin/reclassify-events за период до 31 суток.
 */
import { useState, type ReactNode } from "react";

import { api, errorText, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { Icon, type IconName } from "../components/Icons";
import { PageHeader } from "../components/PageHeader";
import { Loaded } from "../components/StateView";
import { fmtDate, fmtDateTime, fmtNumber } from "../format";
import { INCIDENT_GROUPS, title } from "../vocab";

type Out = Schemas["ParametersOut"];
type Values = Schemas["Parameters"];
type Bounds = Out["bounds"];
type Errors = Record<string, string>;

const WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
const NOTIFY_CLASSES = ["alarm", "critical"] as const;
const LIMITS = [
  { key: "sensor_link", label: "Потеря связи канала", unit: "в сутки" },
  { key: "equipment_diag", label: "Диагностика износа", unit: "в сутки" },
  { key: "guard_weekly", label: "Недельная очередь НСД", unit: "в неделю" },
] as const;
const RECLASSIFY_MAX_DAYS = 31;

function getAt(values: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((node, key) => (node as Record<string, unknown> | undefined)?.[key], values);
}

function setAt(values: Values, path: string, value: unknown): Values {
  const next = structuredClone(values);
  const keys = path.split(".");
  const last = keys.pop() as string;
  const parent = keys.reduce<Record<string, unknown>>((node, key) => node[key] as Record<string, unknown>, next as unknown as Record<string, unknown>);
  parent[last] = value;
  return next;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** «с 9:00 до 14:59» — конец окна в схеме не включается: 15 значит «до 14:59». */
function hoursText(from: number, to: number): string {
  if (!Number.isFinite(from) || !Number.isFinite(to) || from >= to) return "—";
  return `с ${from}:00 до ${to - 1}:59`;
}

function daysText(days: number[]): string {
  const set = [...days].sort().join(",");
  if (set === "0,1,2,3,4") return "в будни";
  if (set === "0,1,2,3,4,5,6") return "ежедневно";
  if (set === "5,6") return "в выходные";
  return days.length ? `в ${[...days].sort().map((d) => WEEKDAYS[d]).join(", ")}` : "ни в один день";
}

/** 422 FastAPI: loc ["body", "values", "gas", "alarm_pct"] → «gas.alarm_pct». */
function errorsOf(body: unknown): Errors {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (!Array.isArray(detail)) return {};
  const out: Errors = {};
  for (const item of detail as { loc?: unknown[]; msg?: string }[]) {
    const loc = (item.loc ?? []).filter((part) => part !== "body" && part !== "values").join(".");
    out[loc || "_"] = (item.msg ?? "").replace(/^Value error, /, "");
  }
  return out;
}

export function Settings() {
  const load = useLoad(() => api.GET("/api/v1/settings/parameters"), []);
  return <section className="settings-page">
    <PageHeader eyebrow="Администрирование" title="Настройки" description="Пороги классификации событий СМВУ, подсказки о плановых работах, уведомления и лимит рекомендаций" />
    <Loaded load={load}>{(data) => <SettingsForm key={data.version} initial={data} />}</Loaded>
  </section>;
}

function SettingsForm({ initial }: { initial: Out }) {
  const [saved, setSaved] = useState(initial);
  const [form, setForm] = useState<Values>(initial.values);
  const [errors, setErrors] = useState<Errors>({});
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const locked = saved.locked;
  const dirty = !same(form, saved.values);
  const atVerified = same(form, saved.verified);

  const set = (path: string, value: unknown) => {
    setForm((prev) => setAt(prev, path, value));
    setErrors((prev) => { const next = { ...prev }; delete next[path]; delete next[path.split(".")[0]]; return next; });
    setMessage(null);
  };

  const save = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const { data, error, response } = await api.PUT("/api/v1/settings/parameters", { body: { expected_version: saved.version, values: form } });
      if (data) {
        setSaved(data);
        setForm(data.values);
        setErrors({});
        setMessage({ ok: true, text: `Сохранено, версия ${data.version}. Новые события классифицируются по новым параметрам сразу, лимиты действуют со следующего дневного расчёта. Принятые раньше события — после пересчёта ниже.` });
      } else if (response.status === 422) {
        const found = errorsOf(error);
        setErrors(found);
        const count = Object.keys(found).length;
        setMessage({ ok: false, text: `Не сохранено: ${count === 1 ? "одно поле не прошло проверку" : `не прошли проверку полей: ${count}`}. Исправьте отмеченное и сохраните снова.` });
      } else {
        setMessage({ ok: false, text: errorText(error, response) });
      }
    } catch {
      setMessage({ ok: false, text: errorText(null, undefined) });
    } finally {
      setBusy(false);
    }
  };

  const field = (path: string, label: string, unit: string, step = 1) => <NumberField key={path} path={path} label={label} unit={unit} step={step} value={getAt(form, path) as number} verified={getAt(saved.verified, path) as number} bounds={saved.bounds} error={errors[path]} onChange={(value) => set(path, value)} />;
  const sectionError = (key: string) => errors[key] && <p className="settings-card__error" role="alert">{errors[key]}</p>;
  const state = saved.version === 0 ? "Действуют проверенные значения" : `Версия ${saved.version}${saved.updated_by ? ` · ${saved.updated_by}` : ""}${saved.updated_at ? `, ${fmtDateTime(saved.updated_at)}` : ""}`;

  return <>
    {locked && <div className="settings-lock" role="note"><Icon name="shield" /><div><strong>Стенд: значения только для чтения</strong><p>На демо-стенде настройки закреплены (DEMO_SETTINGS_LOCKED=1), чтобы у всех проверяющих экраны совпадали. Здесь видно, какие параметры действуют; менять их и пересчитывать историю можно на локальной установке.</p><small>{state}</small></div></div>}
    <fieldset className="settings-fieldset" disabled={locked}>
      <div className="settings-grid">
        <Card icon="sensor" eyebrow="Классификация" heading="Метан" hint="Доля метана в объёме воздуха по газовому датчику. От порога тревоги событие — «Тревожное сообщение», от критического — «Критическое».">
          <div className="settings-fields">{field("gas.alarm_pct", "Тревога от", "% объёма", 0.1)}{field("gas.critical_pct", "Критическое от", "% объёма", 0.1)}</div>
          {sectionError("gas")}
        </Card>
        <Card icon="calendar" eyebrow="Подсказка" heading="Газ «похож на плановые»" hint="«Обнаружен газ» в эти часы получает подсказку «вероятно, ППР или ТО»: на будни 9:00–14:59 приходится 88,6 % таких записей. Класс события не меняется.">
          <div className="settings-fields">{field("gas_window.hour_from", "Начало", "ч")}{field("gas_window.hour_to", "Конец, не включая", "ч")}</div>
          <Days label="Дни окна" days={form.gas_window.days} error={errors["gas_window.days"]} onChange={(days) => set("gas_window.days", days)} />
          {sectionError("gas_window")}
          <p className="settings-preview">Подсказка: газ {daysText(form.gas_window.days)} {hoursText(form.gas_window.hour_from, form.gas_window.hour_to)}</p>
        </Card>
        <Card icon="wrench" eyebrow="Подсказка" heading="Серии ППР и ТО" hint="Несколько разных извещателей подряд за окно — так выглядит плановая проверка. Пожарные считаются по объекту, газоанализаторы — по комплексу. Серия засчитывается, если первое событие окна пришлось на рабочее время.">
          <div className="settings-fields settings-fields--three">{field("series.window_min", "Окно серии", "мин")}{field("series.fire_min", "Пожарных извещателей", "шт.")}{field("series.gas_min", "Газоанализаторов", "шт.")}</div>
          <div className="settings-fields">{field("series.work.hour_from", "Рабочее время с", "ч")}{field("series.work.hour_to", "до, не включая", "ч")}</div>
          <Days label="Рабочие дни" days={form.series.work.days} error={errors["series.work.days"]} onChange={(days) => set("series.work.days", days)} />
          {sectionError("series.work")}
          <p className="settings-preview">Серия: от {fmtNumber(form.series.fire_min)} пожарных или {fmtNumber(form.series.gas_min)} газовых за {fmtNumber(form.series.window_min)} мин, {daysText(form.series.work.days)} {hoursText(form.series.work.hour_from, form.series.work.hour_to)}</p>
        </Card>
        <Card icon="bell" eyebrow="Уведомления" heading="Кому приходит «Тревожное сообщение»" hint="Уведомление и всплывающее окно у диспетчера. Предупреждения и неисправности уведомлений не создают.">
          <Checks legend="Классы событий" options={NOTIFY_CLASSES.map((code) => ({ code, label: title("event_class", code) }))} selected={form.notify.classes} error={errors["notify.classes"]} onChange={(codes) => set("notify.classes", codes)} />
          <Checks legend="Группы аварий" options={INCIDENT_GROUPS.map((g) => ({ code: g.code, label: g.title }))} selected={form.notify.groups} error={errors["notify.groups"]} onChange={(codes) => set("notify.groups", codes)} />
          <p className="settings-preview">Группа есть только у критических тревожных сообщений. Событие без группы — метан выше порога, «Обесточен» — уведомляет по классу.</p>
          {form.notify.classes.length === 0 && <p className="settings-card__error" role="status">Уведомления о событиях СМВУ выключены: диспетчер увидит тревоги только в журнале событий.</p>}
        </Card>
        <Card icon="forecast" eyebrow="Прогноз" heading="Лимит рекомендаций" hint="Сколько рекомендаций ML показать и прислать уведомлением: первые N по рангу. Больше проверенного нельзя — точность при таком лимите не проверялась." wide>
          <div className="settings-fields settings-fields--three">{LIMITS.map(({ key, label, unit }) => field(`limits.${key}`, label, unit))}</div>
          <p className="settings-preview">Пауза в 7 дней считается по показанному: рекомендация за лимитом не уходит на паузу и завтра может попасть в выдачу. Недельную очередь ML ставит на паузу в 14 суток по своей выдаче из 4 объектов, объект за лимитом тоже.</p>
        </Card>
      </div>
      {/* На стенде менять нечего: панели действий нет, версия — в плашке сверху. Сообщение
          о сохранении — внутри панели: она прилипает к низу экрана, и ответ виден рядом с кнопкой. */}
      {!locked && <div className="settings-actions">
        <span className="settings-actions__state">{state}{dirty && <b> · есть несохранённые изменения</b>}</span>
        <button type="button" className="button" disabled={atVerified} onClick={() => { setForm(structuredClone(saved.verified)); setErrors({}); setMessage(null); }}>Сбросить к проверенным</button>
        <button type="button" className="button" disabled={!dirty} onClick={() => { setForm(saved.values); setErrors({}); setMessage(null); }}>Отменить изменения</button>
        <button type="button" className="button button--primary" disabled={!dirty || busy} onClick={() => void save()}>{busy ? "Сохранение…" : "Сохранить"}</button>
        {message && <p className={`settings-feedback${message.ok ? "" : " settings-feedback--error"}`} role={message.ok ? "status" : "alert"}>{message.text}{!message.ok && errors._ ? ` ${errors._}` : ""}</p>}
      </div>}
    </fieldset>
    <Reclassify locked={locked} />
  </>;
}

function Card({ icon, eyebrow, heading, hint, wide = false, children }: { icon: IconName; eyebrow: string; heading: string; hint: string; wide?: boolean; children: ReactNode }) {
  return <article className={`panel settings-card${wide ? " settings-card--wide" : ""}`}>
    <header><div><span className="panel__eyebrow">{eyebrow}</span><h2>{heading}</h2><p>{hint}</p></div><Icon name={icon} /></header>
    {children}
  </article>;
}

function NumberField({ path, label, unit, step, value, verified, bounds, error, onChange }: { path: string; label: string; unit: string; step: number; value: number; verified: number; bounds: Bounds; error?: string; onChange: (value: number) => void }) {
  const bound = bounds[path];
  const id = `param-${path}`;
  const out = bound && Number.isFinite(value) && (value < bound.min || value > bound.max);
  const shown = error ?? (out ? `Допустимо от ${fmtNumber(bound.min)} до ${fmtNumber(bound.max)}` : undefined);
  return <div className={`settings-field${shown ? " settings-field--error" : ""}`}>
    <label htmlFor={id}>{label}</label>
    <div className="settings-input"><input id={id} type="number" inputMode="decimal" step={step} min={bound?.min} max={bound?.max} value={Number.isFinite(value) ? value : ""} aria-invalid={shown ? true : undefined} aria-describedby={`${id}-meta`} onChange={(event) => onChange(event.target.value === "" ? Number.NaN : Number(event.target.value))} /><span>{unit}</span></div>
    <small id={`${id}-meta`} className="settings-field__meta">{bound ? `${fmtNumber(bound.min)}–${fmtNumber(bound.max)}` : ""} · проверено {fmtNumber(verified)}{value !== verified && Number.isFinite(value) && <em> · изменено</em>}</small>
    {shown && <small className="settings-field__error" role="alert">{shown}</small>}
  </div>;
}

function Days({ label, days, error, onChange }: { label: string; days: number[]; error?: string; onChange: (days: number[]) => void }) {
  const toggle = (day: number) => onChange(days.includes(day) ? days.filter((d) => d !== day) : [...days, day].sort());
  return <div className={`settings-days${error ? " settings-field--error" : ""}`} role="group" aria-label={label}>
    <span>{label}</span>
    <div>{WEEKDAYS.map((name, day) => <button key={name} type="button" className="day-toggle" aria-pressed={days.includes(day)} onClick={() => toggle(day)}>{name}</button>)}</div>
    {error && <small className="settings-field__error" role="alert">{error}</small>}
  </div>;
}

function Checks<C extends string>({ legend, options, selected, error, onChange }: { legend: string; options: { code: C; label: string }[]; selected: C[]; error?: string; onChange: (codes: C[]) => void }) {
  const toggle = (code: C) => onChange(selected.includes(code) ? selected.filter((c) => c !== code) : options.map((o) => o.code).filter((c) => c === code || selected.includes(c)));
  return <fieldset className="settings-checks">
    <legend>{legend}</legend>
    <div>{options.map((option) => <label key={option.code} className={`settings-check${selected.includes(option.code) ? " settings-check--on" : ""}`}><input type="checkbox" checked={selected.includes(option.code)} onChange={() => toggle(option.code)} />{option.label}</label>)}</div>
    {error && <small className="settings-field__error" role="alert">{error}</small>}
  </fieldset>;
}

function Reclassify({ locked }: { locked: boolean }) {
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  const today = status.data?.demo_today;
  const [range, setRange] = useState<{ from: string; to: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const from = range?.from ?? today ?? "";
  const to = range?.to ?? today ?? "";
  const days = from && to ? Math.round((Date.parse(to) - Date.parse(from)) / 86_400_000) + 1 : 0;
  const invalid = !from || !to ? "Укажите период" : days < 1 ? "Начало позже конца" : days > RECLASSIFY_MAX_DAYS ? `Не больше ${RECLASSIFY_MAX_DAYS} суток за раз` : null;

  const run = async () => {
    setBusy(true);
    setResult(null);
    try {
      const { data, error, response } = await api.POST("/api/v1/admin/reclassify-events", { body: { date_from: from, date_to: to } });
      if (data) {
        const classes = data.classes.map((c) => `${title("event_class", c.old)} → ${title("event_class", c.new)}: ${fmtNumber(c.count)}`).join("; ");
        setResult({ ok: true, text: `${fmtDate(data.date_from)}–${fmtDate(data.date_to)}: событий ${fmtNumber(data.rows)}, изменено ${fmtNumber(data.changed)}${classes ? ` (${classes})` : ""}, подсказок ${fmtNumber(data.hints_changed)}, групп ${fmtNumber(data.groups_changed)}. ${fmtNumber(data.seconds)} с.` });
      } else {
        const found = errorsOf(error);
        setResult({ ok: false, text: response.status === 422 && Object.keys(found).length ? Object.values(found).join(" ") : errorText(error, response) });
      }
    } catch {
      setResult({ ok: false, text: errorText(null, undefined) });
    } finally {
      setBusy(false);
    }
  };

  return <article className="panel settings-card settings-reclassify">
    <header><div><span className="panel__eyebrow">История</span><h2>Пересчёт принятых событий</h2><p>Приём ставит класс и подсказку один раз. События, принятые до изменения, пересчитываются здесь по сохранённым параметрам. Уведомлений по пересчитанным событиям нет.</p></div><Icon name="events" /></header>
    <div className="settings-reclassify__row">
      <label className="field"><span>С</span><input type="date" value={from} disabled={locked} onChange={(event) => setRange({ from: event.target.value, to })} /></label>
      <label className="field"><span>По</span><input type="date" value={to} disabled={locked} onChange={(event) => setRange({ from, to: event.target.value })} /></label>
      <button type="button" className="button button--primary" disabled={locked || busy || !!invalid} onClick={() => void run()}>{busy ? "Пересчёт…" : "Пересчитать"}</button>
    </div>
    <small className="settings-field__meta">{locked ? "На стенде пересчёт закрыт: данные стенда не меняются." : invalid ?? `Суток: ${days}. До ${RECLASSIFY_MAX_DAYS} суток за раз; всю историю пересчитывает scripts/reclassify_events.py.`}</small>
    {result && <p className={`settings-feedback${result.ok ? "" : " settings-feedback--error"}`} role={result.ok ? "status" : "alert"}>{result.text}</p>}
  </article>;
}
