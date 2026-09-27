import { useRef } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { fmtDate } from "../format";
import { useReloadOn, useStream } from "../stream/useStream";
import { SCENARIO_SHORT, title, type Permission } from "../vocab";
import { Icon, type IconName } from "./Icons";
import { headState } from "./StateView";
import { ToastCenter } from "./ToastCenter";
import { THEME_TITLE, useThemeMode } from "../themeMode";

interface MenuItem { to: string; label: string; hint: string; icon: IconName; perm: Permission }
const MENU: MenuItem[] = [
  { to: "/", label: "Центр управления", hint: "Оперативная картина", icon: "dashboard", perm: "view" },
  { to: "/forecasts", label: "Прогнозы", hint: "Очередь рисков", icon: "forecast", perm: "view" },
  { to: "/work-orders", label: "Заявки", hint: "Превентивные работы", icon: "orders", perm: "view" },
  { to: "/events", label: "События", hint: "Поток СМВУ", icon: "events", perm: "view" },
  { to: "/schema", label: "Схема сети", hint: "Объекты и комплексы", icon: "map", perm: "view" },
  { to: "/quality", label: "Качество модели", hint: "Контроль точности", icon: "quality", perm: "view" },
];
/** Короткие состояния для шапки: полные подписи — в title и на дашборде. */
const SHORT_STATE: Partial<Record<string, string>> = {
  ok: "готов", empty_valid: "без кандидатов", no_data: "нет данных", stale: "устарели",
  error: "ошибка", threshold_infeasible: "молчит",
};

export function Layout() {
  const { user, can, logout } = useAuth();
  const location = useLocation();
  const mobileMore = useRef<HTMLDetailsElement>(null);
  const { unread, connection } = useStream();
  const connected = connection === "open";
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  useReloadOn(["run.finished", "alert.new"], status.reload);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-lockup"><div className="brand-mark"><Icon name="activity" /></div><div><strong>Москоллектор</strong><span>Рабочее место ОДС</span></div></div>
      <nav className="side-nav" aria-label="Основные разделы">
        <span className="side-nav__caption">Рабочее пространство</span>
        {MENU.filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} end={item.to === "/"} className="side-nav__link">
          <Icon name={item.icon} /><span><strong>{item.label}</strong><small>{item.hint}</small></span>
        </NavLink>)}
      </nav>
      <div className="sidebar__footer">
        <div className={`connection ${connected ? "connection--ok" : ""}`}><i />{connected ? "Поток данных подключён" : "Переподключение…"}</div>
        {user && <div className="profile"><div className="avatar">{user.name.slice(0, 1).toUpperCase()}</div><div><strong>{user.name}</strong><span>{title("roles", user.role)}</span></div><button type="button" onClick={() => void logout()} title="Выйти"><Icon name="logout" /></button></div>}
      </div>
    </aside>
    <div className="workspace">
      <header className="mobile-top"><div className="brand-mark"><Icon name="activity" /></div><strong>Москоллектор</strong><NavLink to="/notifications" className="notification-button" aria-label={`Уведомления: ${unread}`} onClick={() => { if (mobileMore.current) mobileMore.current.open = false; }}><Icon name="bell" />{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink><ThemeToggle className="mobile-theme" /><button className="mobile-logout" type="button" onClick={() => void logout()} aria-label="Выйти"><Icon name="logout" /></button></header>
      <StatusBar status={status.data} failed={status.status === "error" && !status.data} unread={unread} />
      <main className="content"><Outlet /></main>
      <nav className="mobile-nav" aria-label="Мобильная навигация">
        {MENU.slice(0, 4).filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} end={item.to === "/"} onClick={() => { if (mobileMore.current) mobileMore.current.open = false; }}><Icon name={item.icon} /><span>{item.label.replace("Центр управления", "Обзор")}</span></NavLink>)}
        <details ref={mobileMore} className={`mobile-more ${["/schema", "/quality", "/notifications"].includes(location.pathname) ? "mobile-more--active" : ""}`} onKeyDown={(event) => { if (event.key === "Escape" && mobileMore.current) mobileMore.current.open = false; }}>
          <summary><Icon name="more" /><span>Ещё</span></summary>
          <div className="mobile-more__panel">
            {MENU.slice(4).filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} onClick={() => { if (mobileMore.current) mobileMore.current.open = false; }}><Icon name={item.icon} /><span>{item.label}</span></NavLink>)}
            <NavLink to="/notifications" onClick={() => { if (mobileMore.current) mobileMore.current.open = false; }}><Icon name="bell" /><span>Уведомления</span>{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink>
          </div>
        </details>
      </nav>
      <ToastCenter />
    </div>
  </div>;
}

function StatusBar({ status, failed, unread }: { status?: Schemas["SystemStatus"]; failed: boolean; unread: number }) {
  return <div className="statusbar">
    <div className="statusbar__main"><span className="live-dot" />{status ? <><strong>Демо-контур</strong><span>{fmtDate(status.demo_today)}</span><span className="statusbar__divider" /><span>Историческое воспроизведение</span></> : <span>{failed ? "Статус системы недоступен" : "Получаем состояние системы…"}</span>}</div>
    {status && <div className="statusbar__heads"><span className={status.ml.reachable ? "status-ok" : "status-bad"}>ML {status.ml.reachable ? (status.ml.mode === "stub" ? "на связи · заглушка" : "на связи") : "недоступна"}</span>{status.heads.map((head) => { const state = headState(head); return <NavLink key={head.head} to={`/scenarios/${head.scenario}`} title={[title("scenario", head.scenario), head.detail].filter(Boolean).join(": ")}>{SCENARIO_SHORT[head.scenario] ?? head.scenario} <span className={`head-state head-state--${state ?? "none"}`}>{state ? SHORT_STATE[state] ?? "—" : "не запускался"}</span></NavLink>; })}</div>}
    <ThemeToggle />
    <NavLink to="/notifications" className="notification-button desktop-only" aria-label={`Уведомления: ${unread}`}><Icon name="bell" />{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink>
  </div>;
}

const THEME_ICON = { system: "contrast", light: "sun", dark: "moon" } as const;

function ThemeToggle({ className = "desktop-only" }: { className?: string }) {
  const { mode, next } = useThemeMode();
  return <button type="button" className={`theme-toggle ${className}`} onClick={next} title={`${THEME_TITLE[mode]} — нажмите, чтобы сменить`} aria-label={`${THEME_TITLE[mode]}. Сменить тему`}><Icon name={THEME_ICON[mode]} /></button>;
}
