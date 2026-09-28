# Сигнатуры сервисов backend (для задачи 3)

Роутеры (`backend/app/routers/*.py`) уже вызывают эти функции. Задача 3 заменяет `raise NotImplementedError` заглушкой или живым минимумом по таблице спецификации. **Сигнатуры не менять.**

| Модуль | Функция | Уровень | Владелец |
|---|---|---|---|
| `system.py` | `status(db, ml: MlClient) -> SystemStatus` | живое | BE-05 |
| `dashboard.py` | `summary(db) -> DashboardSummary` | живое | BE-05 |
| `forecasts.py` | `list_forecasts(db, *, scenario, date_from, date_to, decision, outcome, obj, group_by, page, page_size) -> Page[ForecastItem]` | живое | BE-05 |
| `forecasts.py` | `get_card(db, forecast_id) -> ForecastCard \| None` | живое | BE-05 |
| `forecasts.py` | `summary(db, *, scenario, date_from, date_to, decision, outcome, obj, group_by) -> ForecastSummary` | живое | FE-03 |
| `decisions.py` | `create(db, forecast_id, body: DecisionIn, user) -> DecisionOut` | живое | BE-06 |
| `decisions.py` | `set_outcome(db, forecast_id, body: OutcomeIn, user) -> OutcomeOut` | живое | BE-06 |
| `reference.py` | `reason_codes(db) -> list[ReasonCodeOut]` | живое | BE-03 |
| `reference.py` | `tree(db) -> list[TreeNode]` | живое | BE-03 |
| `reference.py` | `sync(db, user) -> SyncReport` | живое | BE-14 |
| `work_orders.py` | `list_orders(db, *, status, priority, scenario, page, page_size) -> Page[WorkOrderItem]` | живое | BE-06 |
| `work_orders.py` | `get(db, order_id) -> WorkOrderCard \| None` | живое | BE-06 |
| `work_orders.py` | `create(db, body: WorkOrderCreate, user) -> WorkOrderCard` | живое | BE-06 |
| `work_orders.py` | `transition(db, order_id, body: WorkOrderTransition, user) -> WorkOrderCard` | живое | BE-06 |
| `events.py` | `list_events(db, *, date_from, date_to, obj, sensor_type, event_class, q, page, page_size, hide_normal_gas=False, incident_group=None, sort="ts", order="desc") -> Page[EventItem]` — `sort` кроме `ts` роутер пускает только при `sort_range_ok` | живое | ML2-03 |
| `ingest.py` | `ingest_rows(db, rows: list[EventRowIn], user, *, notify: bool = True) -> IngestBatchOut` | живое | ML2-03 |
| `ingest.py` | `ingest_file(db, filename: str, content: bytes, user, *, notify: bool = True) -> IngestBatchOut` | живое | ML2-03 |
| `ingest.py` | `mark_series(db, fresh: list[Event], channels: dict[int, ChannelInfo]) -> int` — подсказка серии ППР/ТО новым событиям пачки до `db.add` и UPDATE уже записанных; возвращает число обновлённых | живое | ML2-01 |
| `ingest.py` | `reset_day(db, day: date, user) -> ResetDayOut` | живое | ML2-03 |
| `ingest.py` | `ingest_ods(db, rows: list[OdsRowIn], user) -> IngestBatchOut` | живое | BE-06 |
| `ingest.py` | `list_batches(db, page, page_size) -> Page[IngestBatchOut]` | живое | ML2-03 |
| `semantics.py` | `classify(sensor_type, val_raw, val_num, alarm, *, ts=None) -> Verdict` — (event_class, hint, incident_group); `ts` нужен для подсказки «вероятно, ППР или ТО» о газе в рабочие часы | живое | ML2-01 |
| `semantics.py` | `series_hints(events: Iterable[SeriesEvent]) -> dict[ref, str]` — подсказка серии ППР/ТО по событиям групп `fire` и `gas` | живое | ML2-01 |
| `geo.py` | `schema_geojson(db, complex_id) -> FeatureCollection` | живое | ML1-11 |
| `geo.py` | `schema_wkt(db, complex_id) -> str` | живое | ML1-11 |
| `quality.py` | `weekly(db, scenario) -> QualityOut` | живое | BE-05 |
| `notifications.py` | `list_notifications(db, user, page, page_size) -> Page[NotificationItem]` | живое | BE-08 |
| `notifications.py` | `mark_read(db, notification_id, user) -> None` | живое | BE-08 |
| `export.py` | `forecasts_xlsx(db, date_from, date_to) -> bytes` | живое | BE-11 |
| `report.py` | `period(db, date_from, date_to) -> tuple[date, date]` — период отчёта по умолчанию и 422; `collect(db, date_from, date_to) -> ReportData` | живое | BE-11 |
| `report_pdf.py` | `render(data: ReportData) -> bytes` | живое | BE-11 |
| `audit_query.py` | `list_audit(db, *, user_login, date_from, date_to, page, page_size) -> Page[AuditItem]` | живое | BE-09 |
| `settings_store.py` | `get(db) -> SettingsOut` | живое | BE-05 |
| `settings_store.py` | `put(db, body: SettingsIn, user) -> SettingsOut` | живое; 403 `settings_locked` при `DEMO_SETTINGS_LOCKED=1` | BE-05 |
| `daily_run.py` | `run_daily(db, asof: date, ml: MlClient, *, weekly_only: bool = False) -> RunDailyOut` | живое | PM-09 |
| `daily_run.py` | `clear_issued_log(db, date_from: date, date_to: date) -> int` | живое | PM-09 |
| `emulation.py` | `emulate(db, body: EmulateDecisionsIn) -> EmulateDecisionsOut` | живое | PM-09 |
