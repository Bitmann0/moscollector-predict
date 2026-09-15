const state = { forecasts: [] };
const labels = { critical: "Критический", high: "Высокий", medium: "Средний", low: "Низкий" };
const fmt = new Intl.NumberFormat("ru-RU");

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) throw new Error((await response.json()).detail || "Ошибка запроса");
  return response.json();
}

function metric(label, value, note) {
  return `<div class="metric"><span>${label}</span><b>${value}</b><small>${note}</small></div>`;
}

function renderMetrics(summary) {
  document.querySelector("#metrics").innerHTML = [
    metric("Активные каналы", fmt.format(summary.channel_count || 0), `${fmt.format(summary.event_count || 0)} событий обработано`),
    metric("Высокий риск", fmt.format(summary.risk_counts.high + summary.risk_counts.critical), "нужна проверка в смену"),
    metric("Максимальный риск", `${Math.round(summary.max_risk_score * 100)}%`, `горизонт ${summary.forecast_horizon_hours} часа`),
    metric("Заявки на ТО", fmt.format(summary.maintenance_requests), "черновики диспетчера"),
  ].join("");
}

function renderMap(items) {
  const visible = items.filter(x => ["critical", "high", "medium"].includes(x.risk_level)).slice(0, 80);
  document.querySelector("#risk-map").querySelectorAll(".map-point").forEach(x => x.remove());
  visible.forEach(item => {
    const point = document.createElement("button");
    point.className = `map-point ${item.risk_level}`;
    point.style.left = `${item.map_x}%`; point.style.top = `${item.map_y}%`;
    point.title = `${item.sensor_name}: ${Math.round(item.risk_score * 100)}%`;
    point.onclick = () => focusRow(item.channel_id);
    document.querySelector("#risk-map").appendChild(point);
  });
}

function renderQueue(items) {
  const priority = items.filter(x => x.risk_level !== "low").slice(0, 12);
  document.querySelector("#queue-count").textContent = priority.length;
  document.querySelector("#risk-queue").innerHTML = priority.map(item => `
    <div class="risk-card" onclick="focusRow(${item.channel_id})">
      <div class="risk-card-top"><b>${item.sensor_name}</b><span class="score ${item.risk_level}">${Math.round(item.risk_score * 100)}%</span></div>
      <p>${item.location} · ${item.factors[0]}</p>
    </div>`).join("") || '<div class="risk-card"><p>Повышенные риски не найдены</p></div>';
}

function renderTable(items) {
  document.querySelector("#forecast-table").innerHTML = items.map(item => `
    <tr id="channel-${item.channel_id}">
      <td><b>${item.sensor_name}</b><small>Канал ${item.channel_id} · ${item.sensor_type}</small></td>
      <td>${item.location}</td><td>${item.factors[0]}</td>
      <td><span class="pill ${item.risk_level}">${labels[item.risk_level]} · ${Math.round(item.risk_score * 100)}%</span></td>
      <td><button class="action" onclick="createRequest(${item.channel_id})">В заявку</button></td>
    </tr>`).join("");
}

function applyFilters() {
  const q = document.querySelector("#search").value.toLowerCase();
  const level = document.querySelector("#level").value;
  const items = state.forecasts.filter(x => (!level || x.risk_level === level) &&
    (!q || `${x.channel_id} ${x.sensor_name} ${x.sensor_type} ${x.location}`.toLowerCase().includes(q)));
  renderTable(items.slice(0, 300));
}

function focusRow(id) {
  const row = document.querySelector(`#channel-${id}`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
}

function toast(message) {
  const el = document.querySelector("#toast"); el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

async function createRequest(channelId) {
  const result = await api("/api/v1/maintenance-requests", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({channel_id: channelId})});
  toast(result.created ? "Черновик заявки сформирован" : "Черновик уже существует");
  await refreshSummary();
}

async function refreshSummary() { renderMetrics(await api("/api/v1/summary")); }

async function init() {
  try {
    const [health, result] = await Promise.all([api("/api/v1/health"), api("/api/v1/forecasts?limit=5000")]);
    state.forecasts = result.items;
    document.querySelector("#updated").textContent = health.data_to ? `Данные по ${new Date(health.data_to).toLocaleString("ru-RU")}` : "Данные не загружены";
    await refreshSummary(); renderMap(state.forecasts); renderQueue(state.forecasts); renderTable(state.forecasts.slice(0, 300));
  } catch (error) { document.querySelector("#notice").textContent = error.message; }
}

document.querySelector("#search").addEventListener("input", applyFilters);
document.querySelector("#level").addEventListener("change", applyFilters);
document.querySelector("#auto-create").addEventListener("click", async () => {
  const result = await api("/api/v1/maintenance-requests/auto", {method: "POST"});
  toast(`Создано заявок: ${result.created}`); await refreshSummary();
});
window.focusRow = focusRow; window.createRequest = createRequest; init();

