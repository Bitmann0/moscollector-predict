const historyState = { offset: 0, limit: 100, total: 0, sequence: 0, channelSequence: 0 };
const historyNumber = value => value == null ? "—" : fmt.format(value);

function historyPaging() {
  document.querySelector("#history-prev").disabled = historyState.offset === 0;
  document.querySelector("#history-next").disabled = historyState.offset + historyState.limit >= historyState.total;
  document.querySelector("#history-page").textContent = historyState.total
    ? `${historyState.offset + 1}–${Math.min(historyState.offset + historyState.limit, historyState.total)} из ${fmt.format(historyState.total)}`
    : "Нет наблюдений за выбранную дату";
}

async function loadHistory() {
  const sequence = ++historyState.sequence;
  const day = document.querySelector("#history-date").value;
  if (!day) return;
  const params = new URLSearchParams({day, limit: historyState.limit, offset: historyState.offset});
  const channel = document.querySelector("#history-channel").value;
  if (channel) params.set("channel", channel);
  document.querySelector("#history-status").textContent = "Загружаем наблюдения…";
  document.querySelector("#history-prev").disabled = true;
  document.querySelector("#history-next").disabled = true;
  try {
    const result = await api(`/api/v1/history/days?${params}`);
    if (sequence !== historyState.sequence) return;
    historyState.total = result.total;
    document.querySelector("#history-status").textContent = `Наблюдения за ${day}. Суточные признаки доступны после окончания этого дня. Сначала наибольшее увеличение активности относительно прошлого.`;
    document.querySelector("#history-table").innerHTML = result.items.map(item => `
      <tr><td><button class="action" onclick="openHistoryChannel(${item.channel_id}, '${day}')">Канал ${item.channel_id}</button><small>${escapeHtml(item.sensor_type || "Нет типа в справочнике")}</small></td>
      <td>${historyNumber(item.event_count)}</td><td>${historyNumber(item.alarm_count)}</td>
      <td>${historyNumber(item.median_events_previous_30d)}</td>
      <td>${item.baseline_available && item.activity_ratio_to_past != null ? `${item.activity_ratio_to_past.toFixed(2)}×` : "Недостаточно истории"}</td>
      <td>${item.observed_days_previous_30d} / 30</td></tr>`).join("");
    historyPaging();
  } catch (error) {
    if (sequence !== historyState.sequence) return;
    document.querySelector("#history-status").textContent = error.message;
    document.querySelector("#history-table").textContent = "";
    historyState.total = 0; historyState.offset = 0; historyPaging();
  }
}

async function openHistoryChannel(channel, day) {
  const sequence = ++historyState.channelSequence;
  const dialog = document.querySelector("#history-dialog");
  document.querySelector("#history-title").textContent = `Канал ${channel} · последние 30 дней по ${day}`;
  document.querySelector("#history-coverage").textContent = "Загрузка…";
  document.querySelector("#history-series").textContent = "";
  dialog.showModal();
  try {
    const result = await api(`/api/v1/history/channels/${channel}?end=${day}&days=30`);
    if (sequence !== historyState.channelSequence) return;
    document.querySelector("#history-coverage").textContent = `Дней с наблюдениями: ${result.observed_days}; без наблюдений: ${result.unobserved_days}. Отсутствие событий не доказывает отсутствие неисправности.`;
    document.querySelector("#history-series").innerHTML = result.items.map(item => `<tr><td>${escapeHtml(item.local_date)}</td><td>${historyNumber(item.event_count)}</td><td>${historyNumber(item.alarm_count)}</td><td>${historyNumber(item.days_since_previous)}</td></tr>`).join("");
  } catch (error) { document.querySelector("#history-coverage").textContent = error.message; }
}

document.querySelector("#history-filter").onsubmit = event => { event.preventDefault(); historyState.offset = 0; loadHistory(); };
document.querySelector("#history-prev").onclick = () => { historyState.offset = Math.max(0, historyState.offset - historyState.limit); loadHistory(); };
document.querySelector("#history-next").onclick = () => { historyState.offset += historyState.limit; loadHistory(); };
document.querySelector("#history-close").onclick = () => { historyState.channelSequence++; document.querySelector("#history-dialog").close(); };

(async () => {
  try {
    const summary = await api("/api/v1/history/summary");
    document.querySelector("#history-summary").textContent = `${summary.date_from} — ${summary.date_to} · ${fmt.format(summary.channels)} каналов · ${fmt.format(summary.observed_channel_days)} наблюдавшихся канал-дня`;
    const dateInput = document.querySelector("#history-date");
    dateInput.min = summary.date_from; dateInput.max = summary.date_to; dateInput.value = summary.date_to;
    await loadHistory();
  } catch (error) { document.querySelector("#history-status").textContent = error.message; }
})();
