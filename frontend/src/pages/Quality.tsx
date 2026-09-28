import { useSearchParams } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { PageHeader } from "../components/PageHeader";
import { SourceBadge } from "../components/common";
import { Loaded } from "../components/StateView";
import { fmtNumber, fmtPercent } from "../format";
import { SCENARIOS, type Scenario } from "../vocab";

type Week = Schemas["QualityWeek"];

const DEFAULT_SCENARIO: Scenario = "sensor_link";
const TICKS = [0, 0.25, 0.5, 0.75, 1];

function plural(count: number, one: string, few: string, many: string): string {
  const mod10 = count % 10, mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

const count = (value: number, one: string, few: string, many: string) => `${fmtNumber(value)} ${plural(value, one, few, many)}`;

/** Начало и конец недели «01.06», «07.06»; дата без Date — часовой пояс браузера не сдвинет день. */
function weekParts(start: string): [string, string] {
  const [y, m, d] = start.split("-").map(Number);
  const end = new Date(Date.UTC(y, m - 1, d + 6));
  const pad = (n: number) => String(n).padStart(2, "0");
  return [`${pad(d)}.${pad(m)}`, `${pad(end.getUTCDate())}.${pad(end.getUTCMonth() + 1)}`];
}

const weekRange = (start: string) => weekParts(start).join("–");

/** Разница в десятых долях процента — из тех же округлённых значений, что стоят в карточках: 41,2 % и 8,7 % дают «+32,5 п.п.», а не «+32». */
const deltaTenths = (rule: number, base: number) => Math.round(rule * 1000) - Math.round(base * 1000);

function deltaText(tenths: number): string {
  return `${tenths > 0 ? "+" : tenths < 0 ? "−" : ""}${fmtNumber(Math.abs(tenths) / 10)} п.п. к базе`;
}

/** Коды исходов из заметки backend («unknown» и т. п.) показываем словами интерфейса. */
function humanNote(note: string): string {
  return note.replace(/\bunknown\b/g, "«неизвестно»").replace(/\bmiss\b/g, "«промах»").replace(/\bhit\b/g, "«попадание»");
}

export function Quality() {
  const [params, setParams] = useSearchParams();
  const scenario = SCENARIOS.find((s) => s.code === params.get("scenario"))?.code ?? DEFAULT_SCENARIO;
  const load = useLoad(() => api.GET("/api/v1/quality", { params: { query: { scenario } } }), [scenario]);
  return <section>
    <PageHeader eyebrow="Контроль модели" title="Качество прогноза" description="Сколько выданных прогнозов подтвердилось за четыре полные недели до текущей" actions={<label className="field quality-select"><span>Сценарий</span><select value={scenario} onChange={(e) => setParams({ scenario: e.target.value })}>{SCENARIOS.map((s) => <option key={s.code} value={s.code}>{s.title}</option>)}</select></label>} />
    <Loaded load={load}>{(data) => {
      const hit = data.weeks.reduce((sum, w) => sum + w.hit, 0), miss = data.weeks.reduce((sum, w) => sum + w.miss, 0), unknown = data.weeks.reduce((sum, w) => sum + w.unknown, 0);
      const checked = hit + miss;
      const rule = data.rule_precision ?? null, base = data.base_rate ?? null;
      // Откуда взяты база и правило (реестр метрик ML1-08): период, отчёт ML и пояснение.
      const { reference_period: period, reference_source: source, reference_note: refNote } = data;
      // С пояснением из реестра null у правила значит «в продукте работает само правило», без него — «ещё не рассчитано».
      const ruleIsProduct = rule == null && !!refNote;
      // Дельта только при обоих числах: при null сравнения нет, и «0 п.п.» было бы неправдой.
      const delta = rule != null && base != null ? deltaTenths(rule, base) : null;
      return <>
        <div className="quality-hero">
          {/* Точность модели за всё окно — та же формула, что у недель: попадания / (попадания + промахи). */}
          <article className="quality-score quality-score--model"><span>Модель за {count(data.weeks.length, "неделю", "недели", "недель")}</span>{checked ? <strong>{fmtPercent(hit / checked)}</strong> : <strong className="quality-score__none">Нет данных</strong>}<small>{checked ? `${count(hit, "попадание", "попадания", "попаданий")} из ${fmtNumber(checked)} проверенных${unknown ? ` · ещё ${fmtNumber(unknown)} с неизвестным исходом` : ""}` : "Проверенных исходов пока нет — точность не из чего считать"}</small></article>
          {/* null от backend — значение ещё не рассчитано, а не ноль: так и пишем. */}
          <article className="quality-score"><span>Простое правило</span>{rule != null ? <strong>{fmtPercent(rule)}</strong> : <strong className="quality-score__none">{ruleIsProduct ? "Сравнения нет" : "Нет данных"}</strong>}<small>{rule != null ? "Точность правила для сравнения, не модели" : ruleIsProduct ? "В продукте работает само правило — сравнивать его не с чем. Подробности ниже, в «Опорных числах»" : "Точность правила для этого сценария ещё не рассчитана"}</small>{delta != null && <em className={`quality-delta quality-delta--${delta < 0 ? "decline" : delta > 0 ? "gain" : "flat"}`} title="Разница точности правила и базовой частоты, процентные пункты">{deltaText(delta)}</em>}</article>
          <article className="quality-score"><span>Базовая частота</span>{base != null ? <strong>{fmtPercent(base)}</strong> : <strong className="quality-score__none">Нет данных</strong>}<small>{base != null ? "Точность случайного выбора объектов" : "Базовая частота для этого сценария ещё не рассчитана"}</small></article>
        </div>
        {period && (rule != null || base != null) && <p className="quality-period">{rule != null && base != null ? "Простое правило и базовая частота" : rule != null ? "Простое правило" : "Базовая частота"} — из расчёта ML за {period}; точность модели — за недели на графике ниже.</p>}
        <div className="quality-layout">
          <article className="panel quality-chart"><header><div><span className="panel__eyebrow">Динамика</span><h3>Точность по неделям</h3></div><div className="quality-chart__aside">{base != null && <span className="quality-chart__key"><i aria-hidden="true" />базовая частота {fmtPercent(base)}</span>}<SourceBadge source={data.source} /></div></header><WeeklyPrecision weeks={data.weeks} base={base} />{base == null && <p className="quality-chart__note">Линия базовой частоты появится, когда базовая частота будет рассчитана.</p>}</article>
          <article className="panel quality-outcomes"><header><div><span className="panel__eyebrow">Проверка</span><h3>Исходы прогнозов</h3></div></header>
            <ul className="quality-legend" aria-label="Обозначения"><li><i className="quality-stack__hit" />Попадание</li><li><i className="quality-stack__miss" />Промах</li><li><i className="quality-stack__unknown" />Неизвестно</li></ul>
            <div className="quality-outcomes__list">{data.weeks.map((week) => <div key={week.week_start}><div className="quality-outcomes__head"><strong>{weekRange(week.week_start)}</strong><span>{count(week.issued, "прогноз", "прогноза", "прогнозов")}</span></div>{week.issued > 0 ? <><div className="quality-stack" role="img" aria-label={`Попадания ${week.hit}, промахи ${week.miss}, неизвестно ${week.unknown}`}>{week.hit > 0 && <i className="quality-stack__hit" style={{ flexGrow: week.hit }} />}{week.miss > 0 && <i className="quality-stack__miss" style={{ flexGrow: week.miss }} />}{week.unknown > 0 && <i className="quality-stack__unknown" style={{ flexGrow: week.unknown }} />}</div><small><b>{count(week.hit, "попадание", "попадания", "попаданий")}</b> · {count(week.miss, "промах", "промаха", "промахов")} · {fmtNumber(week.unknown)} неизвестно</small></> : <small>Прогнозов на этой неделе не выдавалось</small>}</div>)}</div>
          </article>
        </div>
        <div className="quality-method"><strong>Как читать метрику</strong>{data.note && <p>{humanNote(data.note)}</p>}<p>Точность (precision) недели — попадания / (попадания + промахи) среди выданных прогнозов. «Неизвестно» — по данным СМВУ день не наблюдаем или окно прогноза ещё не закрылось; такие прогнозы в точность не входят.</p>
          {(period || refNote || source) && <div className="quality-method__ref"><strong>Опорные числа</strong>{period && <p>Период: {period}.</p>}{refNote && <p>{refNote}</p>}{source && <p className="quality-method__source">Источник: <span>{source}</span></p>}</div>}
        </div>
      </>;
    }}</Loaded>
  </section>;
}

/**
 * Столбец на каждую неделю окна. Недели фиксированы, поэтому неделя без
 * проверенных исходов остаётся пустым местом со своей подписью, а не выпадает
 * из оси: линия через пропуск склеила бы соседние недели в одну динамику.
 */
function WeeklyPrecision({ weeks, base }: { weeks: Week[]; base: number | null }) {
  return <figure className="quality-bars">
    <figcaption className="sr-only">Точность по неделям{base != null ? `, базовая частота ${fmtPercent(base)}` : ""}</figcaption>
    <div className="quality-bars__y" aria-hidden="true">{TICKS.map((tick) => <span key={tick} style={{ bottom: `${tick * 100}%` }}>{Math.round(tick * 100)} %</span>)}</div>
    <div className="quality-bars__plot">
      {TICKS.map((tick) => <i key={tick} className={`quality-bars__grid${tick === 0 ? " quality-bars__grid--base" : ""}`} style={{ bottom: `${tick * 100}%` }} />)}
      {base != null && <i className="quality-bars__base" style={{ bottom: `${Math.min(base, 1) * 100}%` }} />}
      {weeks.map((week, index) => {
        const range = weekRange(week.week_start);
        const empty = week.issued === 0 ? "нет прогнозов" : "исходы неизвестны";
        const detail = [...(week.precision != null ? [`точность ${fmtPercent(week.precision)}`, `${count(week.hit, "попадание", "попадания", "попаданий")}, ${count(week.miss, "промах", "промаха", "промахов")}`] : [empty]), ...(week.unknown > 0 ? [`${fmtNumber(week.unknown)} неизвестно`] : [])];
        // Подсказка встаёт сбоку от столбца, к середине графика: не закрывает столбец и не вылезает за край экрана.
        return <div key={week.week_start} className={`quality-bars__slot${index >= weeks.length / 2 ? " quality-bars__slot--end" : ""}`} tabIndex={0} aria-label={`${range}: ${detail.join(", ")}`}>
          {week.precision != null ? <b className="quality-bars__bar" style={{ height: `${week.precision * 100}%` }}><span>{fmtPercent(week.precision)}</span></b> : <em>{empty}</em>}
          <span className="quality-bars__tip" aria-hidden="true"><strong>{range}</strong>{detail.map((line) => <span key={line}>{line}</span>)}</span>
        </div>;
      })}
    </div>
    {/* Две части подписи: на узком экране начало и конец недели встают в две строки, а не наезжают на соседей. */}
    <div className="quality-bars__x" aria-hidden="true">{weeks.map((week) => { const [from, to] = weekParts(week.week_start); return <span key={week.week_start}><b>{from}</b><b>–{to}</b></span>; })}</div>
  </figure>;
}
