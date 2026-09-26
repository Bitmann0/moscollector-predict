import { NavLink, Outlet } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { fmtDate } from "../format";
import { useReloadOn, useStream } from "../stream/useStream";
import { title, type Permission } from "../vocab";
import { Icon, type IconName } from "./Icons";
import { StateView, headState } from "./StateView";
import { ToastCenter } from "./ToastCenter";

interface MenuItem { to: string; label: string; hint: string; icon: IconName; perm: Permission }
const MENU: MenuItem[] = [
  { to: "/", label: "Центр управления", hint: "Оперативная картина", icon: "dashboard", perm: "view" },
  { to: "/forecasts", label: "Прогнозы", hint: "Очередь рисков", icon: "forecast", perm: "view" },
  { to: "/work-orders", label: "Заявки", hint: "Превентивные работы", icon: "orders", perm: "view" },
  { to: "/events", label: "События", hint: "Поток СМВУ", icon: "events", perm: "view" },
  { to: "/schema", label: "Схема сети", hint: "Объекты и комплексы", icon: "map", perm: "view" },
  { to: "/quality", label: "Качество модели", hint: "Контроль точности", icon: "quality", perm: "view" },
];

export function Layout() {
  const { user, can, logout } = useAuth();
  const { unread, connection } = useStream();
  const connected = connection === "open";
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  useReloadOn(["run.finished", "alert.new"], status.reload);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-lockup"><div className="brand-mark"><Icon name="activity" /></div><div><strong>МОСКОЛЛЕКТОР</strong><span>Predictive intelligence</span></div></div>
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
      <header className="mobile-top"><div className="brand-mark"><Icon name="activity" /></div><strong>Москоллектор</strong><NavLink to="/notifications" className="notification-button"><Icon name="bell" />{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink><button className="mobile-logout" type="button" onClick={() => void logout()} aria-label="Выйти"><Icon name="logout" /></button></header>
      <StatusBar status={status.data} failed={status.status === "error" && !status.data} unread={unread} />
      <main className="content"><Outlet /></main>
      <nav className="mobile-nav" aria-label="Мобильная навигация">{MENU.filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} end={item.to === "/"}><Icon name={item.icon} /><span>{item.label.replace("Центр управления", "Обзор").replace("Качество модели", "Качество")}</span></NavLink>)}<button type="button" onClick={() => void logout()}><Icon name="logout" /><span>Выйти</span></button></nav>
      <ToastCenter />
    </div>
  </div>;
}

function StatusBar({ status, failed, unread }: { status?: Schemas["SystemStatus"]; failed: boolean; unread: number }) {
  return <div className="statusbar">
    <div className="statusbar__main"><span className="live-dot" />{status ? <><strong>Демо-контур</strong><span>{fmtDate(status.demo_today)}</span><span className="statusbar__divider" /><span>Историческое воспроизведение</span></> : <span>{failed ? "Статус системы недоступен" : "Получаем состояние системы…"}</span>}</div>
    {status && <div className="statusbar__heads"><span className={status.ml.reachable ? "status-ok" : "status-bad"}>ML {status.ml.reachable ? "на связи" : "недоступна"}</span>{status.heads.map((head) => { const state = headState(head); return <span key={head.head} title={head.detail ?? undefined}>{title("scenario", head.scenario)} · {state ? <StateView state={state} compact /> : "ожидание"}</span>; })}</div>}
    <NavLink to="/notifications" className="notification-button desktop-only" aria-label={`Уведомления: ${unread}`}><Icon name="bell" />{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink>
  </div>;
}
