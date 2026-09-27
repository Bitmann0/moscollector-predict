"""Класс события СМВУ по типу датчика, значению и тревожному признаку."""


def classify(sensor_type: str | None, val_raw: str | None, val_num: float | None,
             alarm: bool) -> tuple[str, str | None]:
    """Возвращает (event_class, hint). event_class — код из vocabularies.json."""
    value = (val_raw or "").strip().casefold()
    if "неисправ" in value or "ошиб" in value or "fault" in value:
        return "fault", None
    if sensor_type == "Газовый датчик" and val_num is not None:
        if val_num >= 5:
            return "critical", "Превышение 5%: требуется немедленная проверка"
        if val_num >= 1:
            return "alarm", "Превышение 1%: проверьте газовую среду"
    if "пожар" in value or "затоп" in value:
        return "critical", None
    if alarm:
        return "alarm", "Вероятно, плановая проверка" if "план" in value else None
    return "normal", None
