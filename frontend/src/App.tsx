/**
 * Маршруты интерфейса. Живое.
 *
 * Все экраны, кроме входа, живут внутри Layout и пускают только вошедшего
 * пользователя с правом view. Адреса совпадают с таблицей раздела Frontend
 * спецификации каркаса; backend отдаёт index.html на любой путь вне /api.
 */
import { Link, Route, Routes } from "react-router-dom";

import { RequireAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { Dashboard } from "./pages/Dashboard";
import { Events } from "./pages/Events";
import { ForecastCard } from "./pages/ForecastCard";
import { Forecasts } from "./pages/Forecasts";
import { Login } from "./pages/Login";
import { Notifications } from "./pages/Notifications";
import { Quality } from "./pages/Quality";
import { Schema } from "./pages/Schema";
import { ScenarioStatus } from "./pages/ScenarioStatus";
import { WorkOrders } from "./pages/WorkOrders";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth perm="view">
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="forecasts" element={<Forecasts />} />
        <Route path="forecasts/:id" element={<ForecastCard />} />
        <Route path="work-orders" element={<WorkOrders />} />
        <Route path="events" element={<Events />} />
        <Route path="schema" element={<Schema />} />
        <Route path="scenarios/:scenario" element={<ScenarioStatus />} />
        <Route path="notifications" element={<Notifications />} />
        <Route path="quality" element={<Quality />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}

function NotFound() {
  return (
    <section>
      <h1>Страница не найдена</h1>
      <p>
        <Link to="/">На обзор</Link>
      </p>
    </section>
  );
}
