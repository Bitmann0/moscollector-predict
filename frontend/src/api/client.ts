/**
 * HTTP-клиент контракта C2. Живое.
 *
 * Пути и типы ответов — из schema.d.ts, его пересобирает `npm run gen:api` из
 * contracts/api_v1.openapi.json. Руками schema.d.ts не правится: CI сверяет его
 * с контрактом и падает на расхождении.
 *
 * Сессия — HttpOnly cookie, её ставит POST /api/v1/auth/login, поэтому
 * credentials: "include". 401 на любом запросе, кроме самого входа, значит, что
 * сессия кончилась: вызывается обработчик из AuthContext, а если его ещё нет —
 * браузер уходит на /login.
 */
import createClient, { type Middleware } from "openapi-fetch";

import type { components, paths } from "./schema";

export type Schemas = components["schemas"];

export const api = createClient<paths>({ baseUrl: "", credentials: "include" });

const LOGIN_PATH = "/api/v1/auth/login";

let unauthorizedHandler: (() => void) | null = null;

/** AuthContext ставит сюда свой выход; null возвращает жёсткий переход на /login. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

function onUnauthorized(): void {
  if (unauthorizedHandler) {
    unauthorizedHandler();
  } else if (window.location.pathname !== "/login") {
    window.location.assign("/login");
  }
}

const redirectOn401: Middleware = {
  onResponse({ response, schemaPath }) {
    if (response.status === 401 && schemaPath !== LOGIN_PATH) onUnauthorized();
    return undefined;
  },
};

api.use(redirectOn401);

/** Коды `detail`, которые backend отдаёт в ошибках, и их текст для диспетчера. */
const DETAIL_TEXT: Record<string, string> = {
  bad_credentials: "Неверный логин или пароль",
  forbidden: "Для вашей роли это действие недоступно",
  forecast_not_found: "Прогноз не найден",
  work_order_not_found: "Заявка не найдена",
  settings_locked: "Настройки демо на стенде закреплены и не меняются",
};

function detailOf(body: unknown): string | null {
  if (typeof body !== "object" || body === null || !("detail" in body)) return null;
  const detail = body.detail;
  return typeof detail === "string" ? detail : null;
}

/**
 * Превращает ответ с ошибкой в фразу для интерфейса. `error` — тело ответа,
 * которое openapi-fetch уже разобрал; у 401/403/404 оно в схеме не описано,
 * поэтому приходит как unknown.
 */
export function errorText(error: unknown, response: Response | undefined): string {
  const detail = detailOf(error);
  if (detail && DETAIL_TEXT[detail]) return DETAIL_TEXT[detail];
  const status = response?.status;
  if (status === undefined) return "Сервер недоступен. Проверьте соединение и повторите.";
  if (status === 401) return "Сеанс завершён, войдите снова";
  if (status === 403) return DETAIL_TEXT.forbidden;
  if (status === 404) return "Запись не найдена";
  if (status === 409) return "Запись уже изменил другой пользователь. Обновите страницу.";
  if (status === 422) return "Проверьте заполнение полей";
  if (status >= 500) return `Ошибка сервера (${status}). Повторите позже.`;
  return detail ?? `Запрос не выполнен (${status})`;
}
