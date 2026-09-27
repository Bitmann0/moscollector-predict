"""Класс события СМВУ по типу датчика, значению и тревожному признаку (C5).

Порядок правил — раздел C5 плана команды:
1. Неисправность датчика: метан меньше 0 или в насыщении 327,68, температура вне
   −60…150, «дата» 01.01.1970 03:00:0x вместо значения.
2. Метан по доле объёма: от 1 % — alarm, от 5 % — critical.
3. Текст состояния и исходный флаг «тревожное».

Подсказка одна — «вероятно, плановая проверка»: «Обнаружен газ» в будни с 9:00 до
14:59 МСК. На это окно приходится 88,6 % таких записей, заказчик подтвердил поверки
баллонами. Сработка остаётся тревогой: подсказка идёт в журнал и в KPI дашборда.
"""
from datetime import datetime

from .helpers import MSK

GAS = "Газовый датчик"
TEMPERATURE = "Датчик температуры"
# Физические пределы — те же, что VALUE_LIMITS в ml/src/mkl/config.py: газ упирается
# в 327,68 = 2^15/100, температура приходит с -3276 и 999 при переполнении.
GAS_LIMITS = (0.0, 327.67)
TEMPERATURE_LIMITS = (-60.0, 150.0)
GAS_ALARM = 1.0
GAS_CRITICAL = 5.0
EPOCH_PREFIX = "01.01.1970 03:00:0"
PLANNED_CHECK_HINT = "вероятно, плановая проверка"


def _planned_check(text: str, ts: datetime | None) -> bool:
    if ts is None or "обнаружен газ" not in text:
        return False
    local = ts.astimezone(MSK) if ts.tzinfo else ts
    return local.weekday() < 5 and 9 <= local.hour < 15


def classify(sensor_type: str | None, val_raw: str | None, val_num: float | None,
             alarm: bool, *, ts: datetime | None = None) -> tuple[str, str | None]:
    """Возвращает (event_class, hint). event_class — код из vocabularies.json.

    ts — время регистрации события; без него подсказка о плановой проверке не ставится.
    """
    text = (val_raw or "").strip()
    lowered = text.casefold()
    if text.startswith(EPOCH_PREFIX):
        return "fault", None
    if sensor_type == GAS and val_num is not None:
        if not GAS_LIMITS[0] <= val_num <= GAS_LIMITS[1]:
            return "fault", None
        if val_num >= GAS_CRITICAL:
            return "critical", None
        if val_num >= GAS_ALARM:
            return "alarm", None
    if (sensor_type == TEMPERATURE and val_num is not None
            and not TEMPERATURE_LIMITS[0] <= val_num <= TEMPERATURE_LIMITS[1]):
        return "fault", None
    if "неисправ" in lowered or "ошиб" in lowered or "fault" in lowered:
        return "fault", None
    if "пожар" in lowered or "затоп" in lowered:
        return "critical", None
    if alarm:
        return "alarm", PLANNED_CHECK_HINT if _planned_check(lowered, ts) else None
    return "normal", None
