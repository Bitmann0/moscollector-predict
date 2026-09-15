from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(r"U:\hackathon")


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

GROUP_OUTAGE_MIN_CHANNELS = 4
GROUP_OUTAGE_WINDOW_MIN = 5
