/**
 * Форматы дат и чисел для интерфейса. Живое.
 *
 * Даты без времени (asof, demo_today) приходят как YYYY-MM-DD и разбираются без
 * Date, чтобы часовой пояс браузера не сдвинул день. Время backend отдаёт с
 * +03:00; показываем его по Москве независимо от пояса браузера.
 */
import type { Schemas } from "./api/client";
import { title } from "./vocab";

const NUMBER = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const PERCENT = new Intl.NumberFormat("ru-RU", { style: "percent", maximumFractionDigits: 1 });
// Приоритет всегда с двумя знаками: иначе в одной колонке соседствуют «1» и «0,71».
const PRIORITY = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const DATE_TIME = new Intl.DateTimeFormat("ru-RU", {
  timeZone: "Europe/Moscow",
  day: "2-digit",
  month: "2-digit",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

const DASH = "—";

export function fmtDate(value: string | null | undefined): string {
  if (!value) return DASH;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  return match ? `${match[3]}.${match[2]}.${match[1]}` : value;
}

export function fmtDateTime(value: string | null | undefined): string {
  if (!value) return DASH;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : DATE_TIME.format(date);
}

export function fmtNumber(value: number | null | undefined): string {
  return value === null || value === undefined ? DASH : NUMBER.format(value);
}

export function fmtPercent(value: number | null | undefined): string {
  return value === null || value === undefined ? DASH : PERCENT.format(value);
}

/** Вклад фактора со знаком: «+0,31» повышает риск, «−0,12» снижает. */
export function fmtSigned(value: number): string {
  const text = NUMBER.format(Math.abs(value));
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

type Forecast = Schemas["ForecastItem"];

/** Номер страницы из строки адреса: не число или меньше 1 — первая. */
export function pageParam(value: string | null): number {
  const page = Number(value);
  return Number.isInteger(page) && page >= 1 ? page : 1;
}

/** Оценка с подписью смысла: вероятность или относительный приоритет. */
export function scoreText(item: Pick<Forecast, "score_type" | "risk" | "priority_score">): string {
  const label = title("score_type", item.score_type);
  if (item.score_type === "probability") return `${fmtPercent(item.risk)} (${label})`;
  const value = item.priority_score;
  return `${value === null || value === undefined ? DASH : PRIORITY.format(value)} (${label})`;
}

export function placeText(item: Pick<Forecast, "object" | "channel">): string {
  const parts = [
    item.object.name ?? item.object.id,
    item.channel?.picket_label,
    item.channel?.name ?? (item.channel ? `канал ${item.channel.id}` : null),
  ];
  return parts.filter(Boolean).join(" · ") || "—";
}

const SHORT_DT = new Intl.DateTimeFormat("ru-RU", {
  day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow",
});

/** Окно прогноза коротко: «01.07, 00:00 — 02.07, 00:00». */
export function fmtWindow(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return "—";
  const a = SHORT_DT.format(new Date(from)), b = SHORT_DT.format(new Date(to));
  // Суточные окна начинаются и кончаются в полночь — время тогда ничего не добавляет.
  const midnight = /,?\s00:00$/;
  if (midnight.test(a) && midnight.test(b)) return `${a.replace(midnight, "")} — ${b.replace(midnight, "")}`;
  return `${a} — ${b}`;
}
