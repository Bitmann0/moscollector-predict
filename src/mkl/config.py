import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Корень проекта: переменная окружения, иначе каталог репозитория, вычисленный
# от расположения этого файла. Абсолютный путь в коде означал, что решение
# запускается ровно на одной машине — для сдачи чужой команде это негодно.
ROOT = Path(os.environ.get("MKL_ROOT", Path(__file__).resolve().parents[2]))


@dataclass(frozen=True)
class Paths:
    root: Path = ROOT
    materials: Path = ROOT / "Materials"
    raw: Path = ROOT / "data" / "raw"
    interim: Path = ROOT / "data" / "interim"
    features: Path = ROOT / "data" / "features"
    tmp: Path = ROOT / "data" / "tmp"
    experiments: Path = ROOT / "experiments"
    models: Path = ROOT / "models"
    reports: Path = ROOT / "reports"

    def ensure(self) -> None:
        for p in (self.raw, self.interim, self.features, self.tmp,
                  self.experiments, self.models, self.reports):
            p.mkdir(parents=True, exist_ok=True)


PATHS = Paths()
PATHS.ensure()

BAD_STATES = frozenset({
    "Неисправен", "Неопределен", "Обесточен", "Отключено устройство",
    "Батарея неисправна", "Батарея разряжена",
})
OK_STATES = frozenset({
    "Норма", "Есть питание", "Устройства на объекте исправны", "Питание от сети",
})
FIRE_STATES = frozenset({"Обнаружен дым", "Обнаружен газ", "Температура выше 40ºC"})
INTRUSION_STATES = frozenset({
    "Обнаружено движение", "Не замкнут", "Рычаг сдернут", "Разбито стекло",
})
WEAR_STATES = frozenset({"Затоплен", "Работают все насосы в АНС"})
ARM_STATES = frozenset({"На охране", "Снято с охраны"})

EQUIPMENT_STYPES = frozenset({
    "Состояние насоса", "Состояние вентилятора", "ИБП",
    "Датчик затопления", "КД Люк", "9-секционный люк",
})

# Периоды, исключаемые из обучения и оценки по указанию заказчика.
# Весной 2021 шла миграция на новую версию СМВУ: сотрудники не могли работать
# в двух системах сразу и не снимали объекты с охраны во время профилактики.
# Из 1 008 147 пожарных тревог 2021 года 1 005 231 приходится на апрель-июнь,
# остальные месяцы в норме, поэтому исключаются именно эти три месяца, а не год.
EXCLUDED_PERIODS = [(date(2021, 4, 1), date(2021, 6, 30))]


def keep_day(day) -> bool:
    """Входят ли сутки в пригодный для обучения период."""
    return not any(a <= day <= b for a, b in EXCLUDED_PERIODS)


HOLDOUT_START = date(2026, 1, 1)
HOLDOUT_END = date(2026, 6, 30)

MAX_FEATURE_WINDOW_DAYS = 30
EMBARGO_DAYS = 1 + MAX_FEATURE_WINDOW_DAYS
EMBARGO_DAYS_WEAR = 7 + MAX_FEATURE_WINDOW_DAYS

MIN_FAILURE_DURATION_S = 3600
# Эпизод длиннее 30 суток — это не отказ, а вывод канала из эксплуатации.
MAX_FAILURE_DURATION_S = 30 * 86400
# Перед отказом канал обязан подавать признаки жизни: если предыдущее событие
# было больше недели назад, он уже спал и предсказывать тут нечего.
MAX_GAP_BEFORE_FAILURE_S = 7 * 86400

# Аппаратные переполнения в числовых каналах: температура приходит со
# значениями -3276 (int16 underflow) и 999, газ упирается в 327.68 = 2^15/100.
# Без отсечки любой остаток и дрейф считаются по мусору.
VALUE_LIMITS = {
    "Датчик температуры": (-60.0, 150.0),
    "Газовый датчик": (0.0, 327.67),
}
GAS_SATURATION = 327.68

GROUP_OUTAGE_MIN_CHANNELS = 4
GROUP_OUTAGE_WINDOW_MIN = 5


# Единственный источник фолбэков конфигурации головы A. Раньше каждый скрипт
# держал свой дефолт, и final_eval по умолчанию мерил метку L6 на окне 2023,
# тогда как эксперименты выбрали L5 на 2019 — расхождение маскировалось тем,
# что файл выбора всегда существовал.
HEAD_A_DEFAULTS = {
    "window_start": "2019-01-01",
    "variant": "L5",
    "horizon_days": "1",
    "eligible_only": "False",
}


def head_a_choice() -> dict:
    """Конфигурация головы A, выбранная экспериментами E0-E2."""
    out = dict(HEAD_A_DEFAULTS)
    path = PATHS.reports / "head_a_choice.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out
