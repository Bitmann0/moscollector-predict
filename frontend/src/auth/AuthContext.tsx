/**
 * Вход, текущий пользователь и права.
 *
 * При старте спрашиваем GET /api/v1/me: cookie HttpOnly, из JS её не видно, и
 * только сервер знает, жива ли сессия. Роль и permissions берём из ответа —
 * их считает backend по матрице vocabularies.json. Поток SSE открывается только
 * для вошедшего пользователя: /stream без сессии отвечает 401.
 */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { api, errorText, setUnauthorizedHandler, type Schemas } from "../api/client";
import { StateView } from "../components/StateView";
import { startStream, stopStream } from "../stream/useStream";
import type { Permission } from "../vocab";

type User = Schemas["UserOut"];
type Status = "loading" | "anonymous" | "authenticated";
type LoginResult = { ok: true } | { ok: false; message: string };

interface AuthValue {
  status: Status;
  user: User | null;
  login: (login: string, password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
  can: (perm: Permission) => boolean;
}

const AuthContext = createContext<AuthValue | null>(null);

/** Состояние, которое кладётся в location.state при уводе на /login. */
export interface LoginRedirectState {
  from?: string;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const statusRef = useRef<Status>(status);
  statusRef.current = status;
  const navigate = useNavigate();

  useEffect(() => {
    let cancelled = false;
    api
      .GET("/api/v1/me")
      .then(({ data }) => {
        if (cancelled) return;
        setUser(data ?? null);
        setStatus(data ? "authenticated" : "anonymous");
      })
      .catch(() => {
        if (!cancelled) setStatus("anonymous");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Сессия кончилась посреди работы: забываем пользователя и уводим на вход,
  // запомнив, откуда ушли. На старте (/me → 401) уводит RequireAuth.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      const wasIn = statusRef.current === "authenticated";
      setUser(null);
      setStatus("anonymous");
      if (wasIn) {
        const from = window.location.pathname + window.location.search;
        const state: LoginRedirectState = { from };
        navigate("/login", { replace: true, state });
      }
    });
    return () => setUnauthorizedHandler(null);
  }, [navigate]);

  useEffect(() => {
    if (status === "authenticated") startStream();
    else if (status === "anonymous") stopStream();
  }, [status]);

  const login = useCallback(async (loginName: string, password: string): Promise<LoginResult> => {
    try {
      const { data, error, response } = await api.POST("/api/v1/auth/login", {
        body: { login: loginName, password },
      });
      if (data) {
        setUser(data);
        setStatus("authenticated");
        return { ok: true };
      }
      if (response.status === 401) return { ok: false, message: "Неверный логин или пароль" };
      return { ok: false, message: errorText(error, response) };
    } catch {
      return { ok: false, message: errorText(null, undefined) };
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.POST("/api/v1/auth/logout");
    } catch {
      // Cookie без сервера не удалить (HttpOnly); локально всё равно выходим.
    }
    stopStream();
    setUser(null);
    setStatus("anonymous");
    navigate("/login", { replace: true });
  }, [navigate]);

  const can = useCallback(
    (perm: Permission) => user !== null && user.permissions.includes(perm),
    [user],
  );

  const value = useMemo<AuthValue>(
    () => ({ status, user, login, logout, can }),
    [status, user, login, logout, can],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth вызван вне AuthProvider");
  return value;
}

/**
 * Пускает на маршрут только вошедшего пользователя с нужным правом.
 * Невошедшего уводит на /login и запоминает адрес, чтобы вернуть после входа.
 */
export function RequireAuth({ perm = "view", children }: { perm?: Permission; children: ReactNode }) {
  const { status, can } = useAuth();
  const location = useLocation();
  if (status === "loading") return <StateView state="loading" />;
  if (status === "anonymous") {
    const state: LoginRedirectState = { from: location.pathname + location.search };
    return <Navigate to="/login" replace state={state} />;
  }
  if (!can(perm)) return <StateView state="forbidden" />;
  return <>{children}</>;
}
