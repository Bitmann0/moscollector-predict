/**
 * Состояние расчёта по сценарию (FE-10): почему прогноз выдан или не выдан.
 *
 * Статус головы — из /system/status. Число прогнозов в очереди и охват — из
 * /dashboard/summary, те же, что на карточке сценария в центре управления: экран
 * открывается оттуда, и цифры на двух экранах должны совпадать.
 */
import type { ReactNode } from "react";
import { Link, useParams } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { Loaded, StateView, headState } from "../components/StateView";
import { fmtDate, fmtDateTime, fmtPercent } from "../format";
import { useReloadOn } from "../stream/useStream";
import { SCENARIOS, SCENARIO_SHORT, title, vocab } from "../vocab";

type Head = Schemas["HeadState"];
type Ml = Schemas["MlReady"];

// Недельная очередь НСД — правило повторяемости тревог (WeeklyResponse.method =
// weekly_recurrence_rule_v1): обучаемой модели и подобранного порога у неё нет, поэтому
// задержка модели и порог для неё не «неизвестны», а не применяются. Считается она
// только по понедельникам (backend/app/services/daily_run.py).
const WEEKLY = "guard_weekly";

// Пожарный риск и подтопление выдаются без порога точности: первые N по риску в сутки
// (ml/configs/heads.yaml, product_policy top_10_per_day и top_5_per_day). «Подобран»
// читался бы как обещание точности, которого у этих сценариев нет.
const NO_PRECISION_GATE = new Set(["fire_risk", "flood_risk"]);

// Что меряет охват: A_link и D оценивают каналы, B и E — объекты (ML считает
// entities_scored по объектам, у B объект оценён, если оценён хотя бы один его участок).
const COVERAGE_TEXT: Record<string, string> = {
  equipment_diag: "доля каналов оборудования, получивших оценку в последнем дневном прогоне",
  fire_risk: "доля объектов, у которых в последнем дневном прогоне оценён хотя бы один участок",
  flood_risk: "доля объектов с насосами, получивших оценку в последнем дневном прогоне",
};

// Готовность ML (ReadyStatus контракта ml_v1) человеческими словами. «ready» — просто
// «На связи»: подробности нужны, только когда расчёт на демо-день не пройдёт.
const ML_NOT_READY: Record<string, string> = {
  missing_data: "На связи, нет данных на демо-день",
  stale: "На связи, данные устарели",
  stale_source: "На связи, данные не обновлялись",
  future_source: "На связи, демо-день позже конца данных",
  error: "На связи, ошибка проверки готовности",
};

function capital(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

// Частица «не» не остаётся в конце строки: «…приоритет, / не вероятность», а не «…не / вероятность».
function glue(text: string): string {
  return text.replace(/ не /g, " не\u00a0");
}

function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

function lagText(code: string, head: Head | undefined): string {
  if (code === WEEKLY) return "Не применяется";
  return head?.model_lag_days == null ? "Нет сведений" : `${head.model_lag_days} сут.`;
}

// Подпись согласована с заголовком состояния threshold_infeasible в StateView.
function thresholdText(code: string, head: Head | undefined): string {
  if (code === WEEKLY) return "Не применяется";
  if (NO_PRECISION_GATE.has(code)) return "Без порога точности — первые по риску";
  if (head?.threshold_feasible === true) return "Подобран";
  if (head?.threshold_feasible === false) return "Недостижим\u00a0— прогнозы не\u00a0выдаются";
  return "Нет сведений";
}

function mlText(ml: Ml): string {
  if (!ml.reachable) return "Недоступен";
  return (ml.status && ML_NOT_READY[ml.status]) || "На связи";
}

// Состояния, при которых последний прогон новых прогнозов по сценарию не выдал.
const SILENT = new Set(["empty_valid", "no_data", "error", "threshold_infeasible", "not_run"]);

function coverageNote(code: string, fraction: number | null | undefined): string {
  if (code === WEEKLY) return "для недельной очереди не считается";
  if (fraction == null) return "нет сведений в последнем дневном прогоне";
  return COVERAGE_TEXT[code] ?? "доля каналов, получивших оценку в последнем дневном прогоне";
}

// Цвет только у отклонения (ISA-101, tokens.css): штатные значения — обычным текстом.
function Row({ label, hint, tone, children }: { label: string; hint?: string; tone?: "warn" | "bad"; children: ReactNode }) {
  return <div>
    <dt>{label}</dt>
    <dd className={tone ? `scenario-status-value--${tone}` : undefined}>{children}</dd>
    {hint && <dd className="scenario-status-hint">{hint}</dd>}
  </div>;
}

function ScenarioSwitch({ code }: { code?: string }) {
  return <nav className="scenario-switch" aria-label="Сценарии расчёта">
    {SCENARIOS.map((item) => <Link key={item.code} to={`/scenarios/${item.code}`} aria-current={item.code === code ? "page" : undefined}>{SCENARIO_SHORT[item.code] ?? item.title}</Link>)}
  </nav>;
}

export function ScenarioStatus() {
  const { scenario: code } = useParams();
  const scenario = vocab.scenario.find((item) => item.code === code);
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  const summary = useLoad(() => api.GET("/api/v1/dashboard/summary"), []);
  useReloadOn(["run.finished"], status.reload);
  useReloadOn(["run.finished", "alert.new"], summary.reload);

  if (!scenario) {
    return <section>
      <PageHeader eyebrow="Состояние расчёта" title="Сценарий не найден" description="В адресе указан сценарий, которого нет. Выберите сценарий из списка." />
      <ScenarioSwitch />
    </section>;
  }

  const reload = () => { status.reload(); summary.reload(); };
  const kpi = summary.data?.scenarios.find((item) => item.scenario === scenario.code);
  // Сводка грузится отдельно от статуса: прочерк без причины читался бы как «нет прогнозов».
  const kpiMissing = summary.data ? "нет сведений в сводке" : summary.status === "error" ? "сводка не загрузилась" : "загрузка…";

  return <section>
    <PageHeader eyebrow="Состояние расчёта" title={scenario.title} description="Причина выдачи или отсутствия прогноза по выбранному направлению" actions={<button className="button" type="button" onClick={reload}>Обновить статус</button>} />
    <ScenarioSwitch code={scenario.code} />
    <Loaded load={status}>{(data) => {
      const head = data.heads.find((item) => item.scenario === scenario.code);
      const state = head ? headState(head) ?? "not_run" : "not_run";
      // Открытый прогноз — в бюджете, без решения и с незакрытым окном (helpers.open_forecast_clauses),
      // поэтому очередь бывает непустой и после прогона без выдачи: там прогнозы прошлых прогонов.
      const pastOnly = SILENT.has(state) && (kpi?.open_forecasts ?? 0) > 0;
      return <div className="scenario-status-grid">
        <article className={`panel scenario-status-main scenario-status-main--${scenario.code}`}>
          <span className="panel__eyebrow">Результат последнего расчёта</span>
          {/* У «ok» в StateView нет пояснения: без строки под заголовком блок выглядит пустым. */}
          <StateView state={state} detail={state === "ok" ? "Расчёт прошёл штатно, прогнозы выданы в очередь диспетчера." : undefined} />
          {/* detail — текст исключения ML или backend как есть, обычно по-английски: диспетчеру хватает пояснения выше, администратору текст нужен для разбора. */}
          {head?.detail && <details className="scenario-status-tech"><summary>Технические подробности</summary><code>{head.detail}</code></details>}
          {scenario.code === WEEKLY && <p className="scenario-status-note">Очередь пересчитывается по понедельникам на неделю вперёд; статус — из последнего такого прогона.</p>}
          {!data.ml.reachable && <p className="filter-notice">Сервис ML сейчас не отвечает. Показан результат прошлого прогона; новый завершится ошибкой, пока связь не восстановится.</p>}
          {data.ml.mode === "stub" && <p className="filter-notice">Сейчас подключена заглушка ML: результат демонстрационный, не эксплуатационный.</p>}
          <div className="scenario-status-facts">
            <div>
              <strong>{kpi ? kpi.open_forecasts : "—"}</strong>
              <span>{kpi ? plural(kpi.open_forecasts, "прогноз", "прогноза", "прогнозов") : "прогнозов"} в очереди</span>
              <small>{kpi ? (pastOnly ? "от прошлых прогонов: окно ещё открыто, решения нет" : "открытые, без решения диспетчера") : kpiMissing}</small>
            </div>
            <div>
              <strong>{fmtPercent(kpi?.coverage_fraction)}</strong>
              <span>охват расчёта</span>
              <small>{kpi ? coverageNote(scenario.code, kpi.coverage_fraction) : kpiMissing}</small>
            </div>
          </div>
          <Link className="button scenario-status-action" to={`/forecasts?scenario=${encodeURIComponent(scenario.code)}`}>Открыть прогнозы сценария →</Link>
        </article>
        <aside className="panel scenario-status-details">
          <h2>Условия расчёта</h2>
          <dl>
            <Row label="Горизонт прогноза">{scenario.horizon_hours} ч</Row>
            <Row label="Тип оценки">{glue(capital(title("score_type", scenario.score_type)))}</Row>
            <Row label="Демо-день">{fmtDate(data.demo_today)}</Row>
            <Row label="Данные для расчёта по">{data.ml.data_last_day ? fmtDate(data.ml.data_last_day) : "Нет сведений"}</Row>
            {/* asof — демо-дата прогона, finished_at — настоящее время сервера: при воспроизведении истории они расходятся на месяцы. */}
            <Row label="Последний прогон" hint={data.last_run?.finished_at ? `завершён ${fmtDateTime(data.last_run.finished_at)} по реальному времени` : undefined}>{data.last_run ? fmtDate(data.last_run.asof) : "Не запускался"}</Row>
            <Row label="Задержка модели" hint={scenario.code === WEEKLY ? "очередь строится правилом, без обучаемой модели" : NO_PRECISION_GATE.has(scenario.code) ? "от конца данных, на которых собран артефакт модели, до даты расчёта" : "от конца данных, на которых подобран порог, до даты расчёта"}>{lagText(scenario.code, head)}</Row>
            <Row label="Порог выдачи">{thresholdText(scenario.code, head)}</Row>
            <Row label="Сервис ML" tone={!data.ml.reachable ? "bad" : data.ml.status && ML_NOT_READY[data.ml.status] ? "warn" : undefined}>{mlText(data.ml)}</Row>
          </dl>
        </aside>
      </div>;
    }}</Loaded>
  </section>;
}
