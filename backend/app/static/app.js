const state = { forecasts: [] };
const labels = { critical: "Критический", high: "Высокий", medium: "Средний", low: "Низкий" };
const fmt = new Intl.NumberFormat("ru-RU");
const escapeHtml = value => String(value).replace(/[&<>"']/g, char => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[char]));
const decisionLabels = {inspection_required: "Нужна проверка", monitor: "Наблюдение", dismissed: "Оценка отклонена", fault_confirmed: "Неисправность подтверждена"};
let selectedChannel = null;

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
    metric("Максимальный приоритет", `${Math.round(summary.max_risk_score * 100)} / 100`, `исторический анализ · ${summary.forecast_horizon_hours} ч`),
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
    point.title = `${item.sensor_name}: ${Math.round(item.risk_score * 100)} / 100`;
    point.onclick = () => focusRow(item.channel_id);
    document.querySelector("#risk-map").appendChild(point);
  });
}

function renderQueue(items) {
  const priority = items.filter(x => x.risk_level !== "low").slice(0, 12);
  document.querySelector("#queue-count").textContent = priority.length;
  document.querySelector("#risk-queue").innerHTML = priority.map(item => `
    <div class="risk-card" onclick="focusRow(${item.channel_id})">
      <div class="risk-card-top"><b>${escapeHtml(item.sensor_name)}</b><span class="score ${item.risk_level}">${Math.round(item.risk_score * 100)}/100</span></div>
      <p>${escapeHtml(item.location)} · ${escapeHtml(item.factors[0])}</p>
    </div>`).join("") || '<div class="risk-card"><p>Повышенные риски не найдены</p></div>';
}

function renderTable(items) {
  document.querySelector("#forecast-table").innerHTML = items.map(item => `
    <tr id="channel-${item.channel_id}">
      <td><b>${escapeHtml(item.sensor_name)}</b><small>Канал ${item.channel_id} · ${escapeHtml(item.sensor_type)}</small></td>
      <td>${escapeHtml(item.location)}</td><td>${escapeHtml(item.factors[0])}</td>
      <td><span class="pill ${item.risk_level}">${labels[item.risk_level]} · ${Math.round(item.risk_score * 100)}/100</span></td>
      <td><button class="action" onclick="focusRow(${item.channel_id})">Решение</button> <button class="action" onclick="createRequest(${item.channel_id})">В заявку</button></td>
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
  selectedChannel = id;
  const item = state.forecasts.find(x => x.channel_id === id);
  document.querySelector("#detail-title").textContent = `${item.sensor_name} · канал ${id}`;
  document.querySelector("#detail-factors").textContent = [...item.factors, item.recommendation].join(". ");
  document.querySelector("#feedback-form").reset();
  document.querySelector("#detail-dialog").showModal();
  loadFeedback(id).catch(error => toast(error.message));
}

async function loadFeedback(id) {
  document.querySelector("#feedback-history").textContent = "Загрузка…";
  const result = await api(`/api/v1/forecasts/${id}/feedback`);
  if (selectedChannel !== id) return;
  document.querySelector("#feedback-history").innerHTML = result.items.map(item => `<p><b>${escapeHtml(decisionLabels[item.decision])}</b> · ${escapeHtml(item.author)} · ${new Date(item.created_at).toLocaleString("ru-RU")}<br>${escapeHtml(item.reason)}</p>`).join("") || "Решений пока нет";
}

async function loadRequests() {
  const result = await api("/api/v1/maintenance-requests");
  document.querySelector("#requests-table").innerHTML = result.items.map(item => `<tr><td>${item.id}</td><td>${escapeHtml(item.sensor_name)}</td><td>${escapeHtml(item.recommendation)}</td><td>${new Date(item.created_at).toLocaleString("ru-RU")}</td></tr>`).join("") || '<tr><td colspan="4">Черновиков пока нет</td></tr>';
}

function toast(message) {
  const el = document.querySelector("#toast"); el.textContent = message; el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
}

async function createRequest(channelId) {
  try {
  const result = await api("/api/v1/maintenance-requests", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({channel_id: channelId})});
  toast(result.created ? "Черновик заявки сформирован" : "Черновик уже существует");
  await refreshSummary(); await loadRequests();
  } catch (error) { toast(error.message); }
}

async function refreshSummary() { renderMetrics(await api("/api/v1/summary")); }

async function init() {
  try {
    const [health, result] = await Promise.all([api("/api/v1/health"), api("/api/v1/forecasts?limit=5000")]);
    state.forecasts = result.items;
    if (!health.ready) document.querySelector("#notice").textContent = health.detail;
    document.querySelector("#updated").textContent = health.data_to ? `Данные по ${new Date(health.data_to).toLocaleString("ru-RU")}` : "Данные не загружены";
    await refreshSummary(); await loadRequests(); renderMap(state.forecasts); renderQueue(state.forecasts); renderTable(state.forecasts.slice(0, 300));
  } catch (error) { document.querySelector("#notice").textContent = error.message; }
}

document.querySelector("#search").addEventListener("input", applyFilters);
document.querySelector("#level").addEventListener("change", applyFilters);
document.querySelector("#auto-create").addEventListener("click", async () => {
  try {
  const result = await api("/api/v1/maintenance-requests/auto", {method: "POST"});
  toast(`Создано заявок: ${result.created}`); await refreshSummary(); await loadRequests();
  } catch (error) { toast(error.message); }
});
document.querySelector("#close-detail").onclick = () => document.querySelector("#detail-dialog").close();
document.querySelector("#feedback-form").onsubmit = async event => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    await api(`/api/v1/forecasts/${selectedChannel}/feedback`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(Object.fromEntries(new FormData(event.target)))});
    await loadFeedback(selectedChannel); toast("Решение сохранено");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
};
document.querySelectorAll(".nav-item").forEach((button, index) => {
  button.onclick = () => {
    document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
    button.classList.add("active");
    document.querySelector(["header", ".map-panel", ".journal-panel", "#requests-panel"][index]).scrollIntoView({behavior:"smooth"});
  };
});
window.focusRow = focusRow; window.createRequest = createRequest; init();
