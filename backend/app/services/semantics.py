"""Класс события СМВУ по типу датчика и значению.

ЗАГЛУШКА — владелец ML2-01 (C5).
Заменить: правила классификации по типу датчика, справочник состояний, склейку
«число + текст», подсказку «вероятно, плановая проверка».
Контракт: classify(sensor_type, val_raw, val_num, alarm) -> (event_class, hint) не
меняется, event_class — код из vocabularies.json; тест tests/test_endpoints_shape.py
должен остаться зелёным.
"""


def classify(sensor_type: str | None, val_raw: str | None, val_num: float | None,
             alarm: bool) -> tuple[str, str | None]:
    """Возвращает (event_class, hint). event_class — код из vocabularies.json."""
    return ("alarm" if alarm else "normal", None)
