/**
 * ЖИВОЕ — владелец FE-01 (C2). Дорабатывать: оформление экрана входа.
 * Контракт: POST /api/v1/auth/login и типы из src/api/schema.d.ts; npm run typecheck
 * должен остаться зелёным. Демо-пароли в интерфейсе и в git не пишем (D13).
 */
import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth, type LoginRedirectState } from "../auth/AuthContext";
import { StateView } from "../components/StateView";

export function Login() {
  const { status, login } = useAuth();
  const location = useLocation();
  const state = location.state as LoginRedirectState | null;
  const returnTo = safeReturnPath(state?.from);

  const [loginName, setLoginName] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (status === "loading") return <StateView state="loading" />;
  // После успешного входа статус становится authenticated, и уводит этот же Navigate.
  if (status === "authenticated") return <Navigate to={returnTo} replace />;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const result = await login(loginName.trim(), password);
    if (!result.ok) {
      setBusy(false);
      setError(result.message);
    }
  }

  return (
    <div className="login">
      <form className="login__form" onSubmit={(event) => void submit(event)}>
        <div className="login__brand"><span>МК</span><div><h1>Москоллектор</h1><small>Predictive intelligence</small></div></div>
        <p className="muted">Единое рабочее место предиктивной эксплуатации инженерной инфраструктуры</p>
        <label className="field">
          <span>Логин</span>
          <input
            name="login"
            autoComplete="username"
            value={loginName}
            onChange={(event) => setLoginName(event.target.value)}
            required
            maxLength={64}
            autoFocus
          />
        </label>
        <label className="field">
          <span>Пароль</span>
          <span className="password-input"><input name="password" type={showPassword ? "text" : "password"} autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required maxLength={200} /><button type="button" onClick={() => setShowPassword((value) => !value)}>{showPassword ? "Скрыть" : "Показать"}</button></span>
        </label>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" className="button button--primary" disabled={busy}>
          {busy ? "Вход…" : "Войти"}
        </button>
        <p className="login__secure">Защищённый доступ · действия пользователей журналируются</p>
      </form>
    </div>
  );
}

/**
 * Адрес возврата после входа. Принимаем только внутренний путь без «//» и «\»:
 * у react-router 6.x открытый редирект через обратную косую черту в useNavigate
 * (npm audit, GHSA-wrjc-x8rr-h8h6), исправление вышло только в 7.x.
 */
function safeReturnPath(from: unknown): string {
  if (typeof from !== "string" || !from.startsWith("/")) return "/";
  if (from.startsWith("//") || from.includes("\\") || from.startsWith("/login")) return "/";
  return from;
}
