"""Сигнатура сервиса (владелец ML2-01). Тело — задача 3 плана каркаса, см. signatures.md."""
def classify(sensor_type: str | None, val_raw: str | None, val_num: float | None,
             alarm: bool) -> tuple[str, str | None]:
    """Возвращает (event_class, hint). event_class — код из vocabularies.json."""
    raise NotImplementedError("каркас: задача 3")
