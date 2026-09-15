const state = { forecasts: [], ready: false, mode: "historical" };
const labels = { critical: "Критический", high: "Высокий", medium: "Средний", low: "Низкий" };
const fmt = new Intl.NumberFormat("ru-RU");
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
})[c]);
const indexLabel = value => `${Math.round(value * 100)}/100`;

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
    metric("Приоритетная проверка", fmt.format(summary.risk_counts.high + summary.risk_counts.critical), "ранжирование по событиям"),
    metric("Максимальный индекс", indexLabel(summary.max_risk_score), "0–100 · не вероятность отказа"),
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
    point.title = `${item.sensor_name}: ${indexLabel(item.risk_score)}`;
    point.onclick = () => focusRow(item.channel_id);
    document.querySelector("#risk-map").appendChild(point);
  });
}

function renderQueue(items) {
  const priority = items.filter(x => x.risk_level !== "low").slice(0, 12);
  document.querySelector("#queue-count").textContent = priority.length;
  document.querySelector("#risk-queue").innerHTML = priority.map(item => `
    <div class="risk-card" onclick="focusRow(${item.channel_id})">
      <div class="risk-card-top"><b>${escapeHtml(item.sensor_name)}</b><span class="score ${item.risk_level}">${indexLabel(item.risk_score)}</span></div>
      <p>${escapeHtml(item.location)} · ${escapeHtml(item.factors[0])}</p>
    </div>`).join("") || '<div class="risk-card"><p>Повышенные риски не найдены</p></div>';
}

function renderTable(items) {
  document.querySelector("#forecast-table").innerHTML = items.map(item => `
    <tr id="channel-${item.channel_id}">
      <td><b>${escapeHtml(item.sensor_name)}</b><small>Канал ${item.channel_id} · ${escapeHtml(item.sensor_type)}</small></td>
      <td>${escapeHtml(item.location)}</td><td>${escapeHtml(item.factors[0])}</td>
      <td><span class="pill ${item.risk_level}">${labels[item.risk_level]} · ${indexLabel(item.risk_score)}</span></td>
      <td><button class="action" ${state.ready ? "" : "disabled"} onclick="createRequest(${item.channel_id})">${state.mode === "historical" ? "Учебный черновик" : "В черновик"}</button></td>
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
  document.querySelector("#search").value = String(id);
  document.querySelector("#level").value = "";
  applyFilters();
  const row = document.querySelector(`#channel-${id}`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
}

function toast(message) {
  const el = document.querySelector("#toast"); el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

async function createRequest(channelId) {
  try {
  const result = await api("/api/v1/maintenance-requests", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({channel_id: channelId})});
  toast(result.created ? "Черновик заявки сформирован" : "Черновик уже существует");
  await refreshSummary(); await refreshRequests();
  } catch (error) { toast(error.message); }
}

async function refreshSummary() {
  const summary = await api("/api/v1/summary");
  state.ready = summary.ready; state.mode = summary.mode;
  document.querySelector("#auto-create").disabled = !state.ready;
  document.querySelector("#auto-create").textContent = state.mode === "historical" ? "Учебные черновики" : "Сформировать черновики";
  document.querySelector("#notice").textContent = !state.ready ? summary.detail :
    `${state.mode === "historical" ? "Исторический просмотр. Черновики учебные." : "Текущие данные."} Индекс 0–100 ранжирует каналы для проверки и не является вероятностью отказа. Прогнозная модель не подключена.`;
  renderMetrics(summary);
}

async function refreshRequests() {
  const result = await api("/api/v1/maintenance-requests");
  document.querySelector("#request-table").innerHTML = result.items.map(item => `
    <tr><td>${item.id}</td><td>${escapeHtml(item.sensor_name)}</td>
    <td>${item.assessment_mode === "historical" ? "Учебный" : item.assessment_mode === "live" ? "Рабочий" : "Режим не указан"}</td>
    <td>${escapeHtml(item.data_as_of || "Нет даты источника")}</td>
    <td>${escapeHtml(item.recommendation)}</td><td>Черновик</td></tr>`).join("") || '<tr><td colspan="6">Черновиков пока нет</td></tr>';
}

async function init() {
  try {
    const health = await api("/api/v1/health");
    state.forecasts = [];
    let result;
    do {
      result = await api(`/api/v1/forecasts?limit=5000&offset=${state.forecasts.length}`);
      state.forecasts.push(...result.items);
    } while (result.items.length && state.forecasts.length < result.total);
    document.querySelector("#updated").textContent = health.data_to ? `Данные по ${new Date(health.data_to).toLocaleString("ru-RU")}` : "Данные не загружены";
    await refreshSummary(); await refreshRequests(); renderMap(state.forecasts); renderQueue(state.forecasts); applyFilters();
  } catch (error) { document.querySelector("#notice").textContent = error.message; }
}

document.querySelector("#search").addEventListener("input", applyFilters);
document.querySelector("#level").addEventListener("change", applyFilters);
document.querySelector("#auto-create").addEventListener("click", async () => {
  try {
  const result = await api("/api/v1/maintenance-requests/auto", {method: "POST"});
  toast(`Создано черновиков: ${result.created}`); await refreshSummary(); await refreshRequests();
  } catch (error) { toast(error.message); }
});
document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("active"));
  button.classList.add("active");
  document.querySelector(button.dataset.target).scrollIntoView({behavior: "smooth"});
}));
window.focusRow = focusRow; window.createRequest = createRequest; init();

