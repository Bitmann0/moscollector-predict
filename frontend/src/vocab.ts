/**
 * Словари C3 из contracts/vocabularies.json. Живое.
 *
 * Файл импортируется при сборке (vite JSON import), поэтому подписи в интерфейсе
 * те же, что у backend (backend/app/vocab.py). Коды совпадают с перечислениями
 * schema.d.ts: это на стороне backend проверяет tests/test_contract_layer.py.
 * Меняется словарь — меняется только JSON, здесь ничего не трогаем.
 */
import raw from "../../contracts/vocabularies.json";

import type { Schemas } from "./api/client";

interface Entry {
  code: string;
  title: string;
  ml_code?: string;
}

interface ScenarioEntry extends Entry {
  head: string;
  direction: string;
  score_type: string;
  horizon_hours: number;
}

interface ReasonEntry extends Entry {
  actions: string[];
}

interface Vocabularies {
  scenario: ScenarioEntry[];
  kind: Entry[];
  score_type: Entry[];
  source: Entry[];
  action: Entry[];
  reason_code: ReasonEntry[];
  outcome_manual: Entry[];
  outcome_auto: Entry[];
  result_status: Entry[];
  work_order_status: Entry[];
  work_order_transitions: Record<string, string[]>;
  work_order_transition_perm: Record<string, string>;
  work_order_priority: Entry[];
  event_class: Entry[];
  incident_group: Entry[];
  roles: Entry[];
  permissions: Record<string, string[]>;
}

export const vocab: Vocabularies = raw;

/** Право роли — ключ матрицы permissions в vocabularies.json. */
export type Permission = keyof typeof raw.permissions;

export type Scenario = Schemas["ForecastItem"]["scenario"];
export type ResultStatus = NonNullable<Schemas["HeadState"]["result_status"]>;
export type Action = Schemas["DecisionIn"]["action"];
export type IncidentGroup = NonNullable<Schemas["EventItem"]["incident_group"]>;

type DictName = {
  [K in keyof Vocabularies]: Vocabularies[K] extends Entry[] ? K : never;
}[keyof Vocabularies];

/** Подпись кода из словаря; неизвестный код показывается как есть, пустой — прочерком. */
export function title(dict: DictName, code: string | null | undefined): string {
  if (code === null || code === undefined || code === "") return "—";
  return vocab[dict].find((item) => item.code === code)?.title ?? code;
}

// Коды словаря совпадают с перечислениями схемы (проверяет backend-тест), поэтому
// приведение типа здесь безопасно.
export const SCENARIOS = vocab.scenario.map((s) => ({ code: s.code as Scenario, title: s.title }));
export const ACTIONS = vocab.action.map((a) => ({ code: a.code as Action, title: a.title }));
/** Группы аварий (ответ 2 заказчика, analysis/qa_customer_2026-09-28.md) в порядке словаря. */
export const INCIDENT_GROUPS = vocab.incident_group.map((g) => ({ code: g.code as IncidentGroup, title: g.title }));

/** Код группы из payload уведомления: там он не типизирован, поэтому сверяем со словарём. */
export function incidentGroupOf(value: unknown): IncidentGroup | undefined {
  return INCIDENT_GROUPS.find((item) => item.code === value)?.code;
}

/** Короткие имена сценариев для плотных мест: шапка, таблицы, метки. */
export const SCENARIO_SHORT: Record<string, string> = {
  sensor_link: "Датчики", equipment_diag: "Износ", guard_weekly: "НСД",
};
