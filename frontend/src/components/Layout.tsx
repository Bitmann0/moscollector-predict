/**
 * Оболочка вошедшего пользователя. Живое; FE-01 дорабатывает вид.
 *
 * Меню строится по правам роли из /me (матрица vocabularies.json → permissions).
 * Плашка демо-времени, связь с моделью и статус сценариев — из /system/status;
 * статус перечитывается, когда по SSE приходит run.finished или alert.new.
 * Колокольчик показывает число непрочитанных событий SSE текущей сессии.
 */
import { NavLink, Outlet } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { fmtDate } from "../format";
import { useReloadOn, useStream } from "../stream/useStream";
import { title, type Permission } from "../vocab";
import { StateView, headState } from "./StateView";

interface MenuItem {
  to: string;
  label: string;
  perm: Permission;
}

/** Пункт виден, если у роли есть право perm; права экранов совпадают с правами их GET в C2. */
const MENU: MenuItem[] = [
  { to: "/", label: "Обзор", perm: "view" },
  { to: "/forecasts", label: "Прогнозы", perm: "view" },
  { to: "/work-orders", label: "Заявки", perm: "view" },
  { to: "/events", label: "События", perm: "view" },
  { to: "/schema", label: "Схема", perm: "view" },
  { to: "/quality", label: "Качество", perm: "view" },
];

export function Layout() {
  const { user, can, logout } = useAuth();
  const { unread } = useStream();
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  useReloadOn(["run.finished", "alert.new"], status.reload);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">Москоллектор</span>
        <nav className="menu" aria-label="Разделы">
          {MENU.filter((item) => can(item.perm)).map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"} className="menu__link">
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="topbar__right">
          <NavLink
            to="/notifications"
            className="bell"
            aria-label={unread > 0 ? `Уведомления: ${unread} новых` : "Уведомления"}
          >
            <BellIcon />
            {unread > 0 && <span className="bell__count">{unread > 99 ? "99+" : unread}</span>}
          </NavLink>
          {user && (
            <span className="user" title={user.login}>
              {user.name}
              {/* У демо-пользователей имя совпадает с названием роли: не повторяем. */}
              {user.name !== title("roles", user.role) && (
                <span className="user__role">{title("roles", user.role)}</span>
              )}
            </span>
          )}
          <button type="button" className="button button--ghost" onClick={() => void logout()}>
            Выйти
          </button>
        </div>
      </header>
      <StatusBar status={status.data} failed={status.status === "error" && !status.data} />
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}

function StatusBar({ status, failed }: { status?: Schemas["SystemStatus"]; failed: boolean }) {
  if (!status) {
    return (
      <div className="statusbar">
        <span className="demo-banner">
          {failed ? "Статус системы недоступен" : "Демо-время: загрузка…"}
        </span>
      </div>
    );
  }
  const ml = status.ml;
  return (
    <div className="statusbar">
      <span className="demo-banner">
        Демо-время: {fmtDate(status.demo_today)} — воспроизведение истории
      </span>
      <span className="statusbar__item">
        Модель:{" "}
        {ml.reachable ? (
          <span className="chip chip--ok">на связи{ml.mode === "stub" ? " (заглушка)" : ""}</span>
        ) : (
          <span className="chip chip--bad">недоступна</span>
        )}
      </span>
      {status.heads.map((head) => {
        const state = headState(head);
        return (
          <span key={head.head} className="statusbar__item">
            {title("scenario", head.scenario)}:{" "}
            {state ? (
              <StateView state={state} detail={head.detail} compact />
            ) : (
              <span className="chip chip--muted">расчёта ещё не было</span>
            )}
          </span>
        );
      })}
      {status.last_run && (
        <span className="statusbar__item">Последний расчёт: {fmtDate(status.last_run.asof)}</span>
      )}
    </div>
  );
}

function BellIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path
        d="M12 3a6 6 0 0 0-6 6v3.6l-1.7 2.9A1 1 0 0 0 5.2 17h13.6a1 1 0 0 0 .9-1.5L18 12.6V9a6 6 0 0 0-6-6Zm-2.5 15.5a2.5 2.5 0 0 0 5 0h-5Z"
        fill="currentColor"
      />
    </svg>
  );
}
