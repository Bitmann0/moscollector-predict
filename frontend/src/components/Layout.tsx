import { useEffect, useRef } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";

import { api, type Schemas } from "../api/client";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/AuthContext";
import { fmtDate } from "../format";
import { useReloadOn, useStream, type Connection } from "../stream/useStream";
import { SCENARIO_SHORT, title, type Permission } from "../vocab";
import { Icon, type IconName } from "./Icons";
import { headState } from "./StateView";
import { ToastCenter } from "./ToastCenter";
import { THEME_TITLE, useThemeMode } from "../themeMode";

/** short — подпись под значком там, где полное название не помещается: узкое боковое меню и нижняя панель. */
interface MenuItem { to: string; label: string; short: string; hint: string; icon: IconName; perm: Permission }
const MENU: MenuItem[] = [
  { to: "/", label: "Центр управления", short: "Обзор", hint: "Оперативная картина", icon: "dashboard", perm: "view" },
  { to: "/forecasts", label: "Прогнозы", short: "Прогнозы", hint: "Очередь рисков", icon: "forecast", perm: "view" },
  { to: "/work-orders", label: "Заявки", short: "Заявки", hint: "Превентивные работы", icon: "orders", perm: "view" },
  { to: "/events", label: "События", short: "События", hint: "Поток СМВУ", icon: "events", perm: "view" },
  { to: "/schema", label: "Схема сети", short: "Схема", hint: "Объекты и комплексы", icon: "map", perm: "view" },
  { to: "/quality", label: "Качество модели", short: "Качество", hint: "Контроль точности", icon: "quality", perm: "view" },
  { to: "/settings", label: "Настройки", short: "Настройки", hint: "Параметры продукта", icon: "sliders", perm: "admin" },
];
/** Разделы, которые на телефоне живут в меню «Ещё»: при них подсвечивается сама кнопка. */
const MORE_PATHS = ["/schema", "/quality", "/settings", "/notifications"];
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
  const status = useLoad(() => api.GET("/api/v1/system/status"), []);
  useReloadOn(["run.finished", "alert.new"], status.reload);
  const closeMore = () => { if (mobileMore.current) mobileMore.current.open = false; };
  // <details> сам не закрывается ни от касания мимо панели, ни от перехода «назад»:
  // без этого меню «Ещё» оставалось открытым поверх нового экрана.
  useEffect(closeMore, [location.pathname]);
  useEffect(() => {
    const onPointer = (event: PointerEvent) => {
      const more = mobileMore.current;
      if (more?.open && event.target instanceof Node && !more.contains(event.target)) more.open = false;
    };
    document.addEventListener("pointerdown", onPointer);
    return () => document.removeEventListener("pointerdown", onPointer);
  }, []);
  // Та же точка и тот же текст, что в строке статуса: иначе при переподключении
  // шапка говорила «переподключается» жёлтым, а меню — другими словами.
  const live = liveState(status.data, status.status === "error" && !status.data, connection);
  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand-lockup"><div className="brand-mark"><Icon name="activity" /></div><div><strong>Москоллектор</strong><span>Рабочее место ОДС</span></div></div>
      <nav className="side-nav" aria-label="Основные разделы">
        <span className="side-nav__caption">Рабочее пространство</span>
        {MENU.filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} end={item.to === "/"} className="side-nav__link">
          <Icon name={item.icon} /><span><strong>{item.label}</strong><small>{item.hint}</small></span><em className="side-nav__short">{item.short}</em>
        </NavLink>)}
      </nav>
      <div className="sidebar__footer">
        <div className={`connection connection--${live.tone}`} title={live.text}><i /><span>{live.text}</span></div>
        {user && <div className="profile"><div className="avatar" title={`${user.name} · ${title("roles", user.role)}`}>{user.name.slice(0, 1).toUpperCase()}</div><div><strong>{user.name}</strong><span>{title("roles", user.role)}</span></div><button type="button" onClick={() => void logout()} title="Выйти" aria-label="Выйти"><Icon name="logout" /></button></div>}
      </div>
    </aside>
    <div className="workspace">
      <header className="mobile-top"><div className="brand-mark"><Icon name="activity" /></div><strong>Москоллектор</strong><NavLink to="/notifications" className="notification-button" aria-label={`Уведомления: ${unread}`}><Icon name="bell" />{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink><ThemeToggle className="mobile-theme" /><button className="mobile-logout" type="button" onClick={() => void logout()} aria-label="Выйти"><Icon name="logout" /></button></header>
      <StatusBar status={status.data} failed={status.status === "error" && !status.data} connection={connection} unread={unread} />
      <main className="content"><Outlet /></main>
      <nav className="mobile-nav" aria-label="Мобильная навигация">
        {MENU.slice(0, 4).filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} end={item.to === "/"}><Icon name={item.icon} /><span>{item.short}</span></NavLink>)}
        <details ref={mobileMore} className={`mobile-more ${MORE_PATHS.includes(location.pathname) ? "mobile-more--active" : ""}`} onKeyDown={(event) => { if (event.key === "Escape") closeMore(); }}>
          <summary><Icon name="more" /><span>Ещё</span></summary>
          <div className="mobile-more__panel">
            {MENU.slice(4).filter((item) => can(item.perm)).map((item) => <NavLink key={item.to} to={item.to} onClick={closeMore}><Icon name={item.icon} /><span>{item.label}</span></NavLink>)}
            <NavLink to="/notifications" onClick={closeMore}><Icon name="bell" /><span>Уведомления</span>{unread > 0 && <b>{unread > 99 ? "99+" : unread}</b>}</NavLink>
          </div>
        </details>
      </nav>
      <ToastCenter />
    </div>
  </div>;
}

/** Точка у «Демо-контур»: на планшете и телефоне подвала бокового меню нет, и живость потока видна только здесь. */
function liveState(status: Schemas["SystemStatus"] | undefined, failed: boolean, connection: Connection): { tone: string; text: string } {
  if (failed) return { tone: "bad", text: "Статус системы недоступен" };
  if (connection === "reconnecting") return { tone: "warn", text: "Поток данных переподключается" };
  if (status && connection === "open") return { tone: "ok", text: "Поток данных подключён" };
  return { tone: "wait", text: "Подключаемся к потоку данных" };
}

function StatusBar({ status, failed, connection, unread }: { status?: Schemas["SystemStatus"]; failed: boolean; connection: Connection; unread: number }) {
  const live = liveState(status, failed, connection);
  return <div className="statusbar">
    <div className="statusbar__main"><span className={`live-dot live-dot--${live.tone}`} role="img" aria-label={live.text} title={live.text} />{status ? <><strong>Демо-контур</strong><span>{fmtDate(status.demo_today)}</span><span className="statusbar__divider" /><span className="statusbar__mode">Историческое воспроизведение</span></> : <span>{failed ? "Статус системы недоступен" : "Получаем состояние системы…"}</span>}</div>
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
