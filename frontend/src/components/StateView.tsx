/**
 * Единый показ состояний экрана и сценария. Отдельные экраны сценариев — FE-10.
 *
 * Коды результата расчёта (ok, empty_valid, no_data, stale, error) и их заголовки —
 * из vocabularies.json → result_status. threshold_infeasible — голова, у которой
 * threshold_feasible = false: она молчит, это не ошибка и не empty_valid
 * (план команды, C1). loading, unavailable, forbidden, empty — состояния самого
 * экрана, а не расчёта.
 */
import type { ReactNode } from "react";

import type { Load } from "../api/useLoad";
import { title, type ResultStatus } from "../vocab";

export type ViewState =
  | ResultStatus
  | "threshold_infeasible"
  | "not_run"
  | "loading"
  | "unavailable"
  | "forbidden"
  | "empty";

type Tone = "ok" | "info" | "warn" | "bad" | "muted";

interface StateText {
  title: string;
  hint?: string;
  tone: Tone;
}

function textOf(state: ViewState): StateText {
  switch (state) {
    case "ok":
      return { title: title("result_status", "ok"), tone: "ok" };
    case "empty_valid":
      return {
        title: title("result_status", "empty_valid"),
        hint: "Расчёт прошёл штатно: подходящих кандидатов в этом расчёте нет.",
        tone: "info",
      };
    case "no_data":
      return {
        title: title("result_status", "no_data"),
        hint: "Для расчёта не хватило наблюдений за нужный период, поэтому прогноз не строился.",
        tone: "warn",
      };
    case "stale":
      return {
        title: title("result_status", "stale"),
        hint: "Данные или модель устарели относительно расчётной даты. Прогноз может не отражать нужный период.",
        tone: "warn",
      };
    case "error":
      return {
        title: title("result_status", "error"),
        hint: "Расчёт по сценарию завершился ошибкой. Сообщите администратору.",
        tone: "bad",
      };
    case "threshold_infeasible":
      return {
        title: "Порог недостижим",
        hint:
          "На истории не нашлось порога, при котором прогнозы достаточно точны, " +
          "поэтому сценарий их не выдаёт. Это не сбой.",
        tone: "muted",
      };
    case "not_run":
      return {
        title: "Расчёт ещё не запускался",
        hint: "Для этого сценария пока нет результата. Отсутствие прогноза не означает отсутствие риска.",
        tone: "muted",
      };
    case "loading":
      return { title: "Загрузка…", tone: "muted" };
    case "unavailable":
      return {
        title: "Не удалось получить данные",
        hint: "Сервер не ответил или вернул ошибку.",
        tone: "bad",
      };
    case "forbidden":
      return { title: "Нет доступа", hint: "Раздел недоступен для вашей роли.", tone: "muted" };
    case "empty":
      return { title: "Записей нет", tone: "muted" };
  }
}

interface Props {
  state: ViewState;
  /** Пояснение от сервера (HeadState.detail, текст ошибки запроса). */
  detail?: string | null;
  onRetry?: () => void;
  /** Короткая метка в строку вместо блока. */
  compact?: boolean;
}

export function StateView({ state, detail, onRetry, compact = false }: Props) {
  const text = textOf(state);
  if (compact) {
    return (
      <span className={`chip chip--${text.tone}`} title={detail ?? text.hint}>
        {text.title}
      </span>
    );
  }
  return (
    <div className={`state state--${text.tone}`} role={state === "loading" ? "status" : undefined}>
      <p className="state__title">{text.title}</p>
      {text.hint && <p className="state__hint">{text.hint}</p>}
      {detail && <p className="state__detail">{detail}</p>}
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Повторить
        </button>
      )}
    </div>
  );
}

/** Состояние сценария по статусу головы из /system/status или /dashboard/summary. */
export function headState(head: {
  result_status?: ResultStatus | null;
  threshold_feasible?: boolean | null;
}): ViewState | null {
  // Сбой или нехватка данных важнее сведений о пороге: иначе можно скрыть ошибку.
  if (head.result_status === "error" || head.result_status === "no_data" || head.result_status === "stale") {
    return head.result_status;
  }
  if (head.threshold_feasible === false) return "threshold_infeasible";
  return head.result_status ?? null;
}

/**
 * Показывает загрузку и ошибку через StateView, а готовые данные отдаёт children.
 * При перезапросе данные остаются на экране, сверху — короткая метка.
 */
export function Loaded<T>({ load, children }: { load: Load<T>; children: (data: T) => ReactNode }) {
  if (load.data === undefined) {
    if (load.status === "error") {
      return <StateView state="unavailable" detail={load.message} onRetry={load.reload} />;
    }
    return <StateView state="loading" />;
  }
  return (
    <>
      {load.status === "loading" && <p className="refreshing">Обновление…</p>}
      {load.status === "error" && (
        <StateView state="unavailable" detail={load.message} onRetry={load.reload} />
      )}
      {children(load.data)}
    </>
  );
}
