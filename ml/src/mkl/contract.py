"""Контракт выдачи: что пайплайн отдаёт наружу.

Отдельный модуль, а не форма возврата serve.score, по одной причине: потребитель
(веб-интерфейс диспетчера) не должен ломаться при переобучении модели, смене
бэкенда или добавлении головы. Всё, что меняется внутри, обязано оставаться
внутри; наружу смотрит эта схема.

Три решения, которые стоит объяснить.

`alert_id` считается детерминированно от головы, сущности и суток. Значит
повторный расчёт тех же суток даёт те же идентификаторы, и потребитель может
обновлять запись, а не плодить дубли. Случайный uuid этого не даёт.

`case_key` — та же сущность без даты. По нему видно, что сегодняшний алерт про
тот же канал, что вчерашний: у нас длинный отказ порождает алерт каждые сутки,
и без такого ключа диспетчер увидит тридцать разных происшествий вместо одного.

`status` различает «риска нет» и «данных нет». У головы несанкционированного
доступа состояние охраны есть на 47 объектах из 78, и по остальным она не
работает вовсе. Молчание в таком случае читается как «всё спокойно», что
неправда.
"""
import datetime as dt
import hashlib
from dataclasses import asdict, dataclass, field

# Версия схемы. Меняется только при несовместимой правке: потребитель вправе
# на неё опираться.
SCHEMA_VERSION = "1.0"

DIRECTIONS = {
    "sensor_failure": "Отказ датчика",
    "fire_risk": "Пожарный риск",
    "unauthorised_access": "Несанкционированный доступ",
    "infrastructure_wear": "Износ инфраструктуры",
    "flood_risk": "Риск подтопления",
    "beyond_scope": "Вне четырёх направлений ТЗ",
}

STATUS_OK = "ok"                # риск посчитан
STATUS_NO_DATA = "no_data"      # сущность вне покрытия головы
STATUS_STALE = "stale"          # данные старше, чем допускает голова


def make_alert_id(head: str, entity: dict, day: dt.date) -> str:
    """Детерминированный идентификатор: та же строка — тот же id."""
    key = "|".join([head, day.isoformat(),
                    *(f"{k}={entity[k]}" for k in sorted(entity))])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def make_case_key(head: str, entity: dict) -> str:
    """Идентификатор происшествия без даты: сегодняшний алерт про тот же
    канал, что вчерашний, получит тот же ключ."""
    key = "|".join([head, *(f"{k}={entity[k]}" for k in sorted(entity))])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Address:
    """Куда ехать. Без этого алерт диспетчеру бесполезен.

    Диспетчер работает названиями, а не идентификаторами: «объект Фита, ПК 28,
    КД АВ», а не «объект 3215». Поэтому рядом с каждым идентификатором лежит
    название из справочника.

    Географических координат в данных нет и не будет — заказчик подтвердил.
    Адресация идёт по объекту, комплексу и пикету: пикет это километровая
    отметка вдоль коллектора, и линейная схема по нему заменяет карту.

    `address_known` отличает «адрес не нашёлся» от «адрес пустой»: 1 142 канала
    из 12 627 есть в журналах, но отсутствуют в справочнике. Пустая строка в
    интерфейсе выглядела бы как отсутствие проблемы.
    """
    # идентификаторы
    obj: str | None = None
    obj_parent: str | None = None
    obj_kind: str | None = None
    channel: int | None = None
    segment: int | None = None
    picket: float | None = None
    # то, что показывают человеку
    obj_name: str | None = None
    obj_parent_name: str | None = None
    obj_kind_ru: str | None = None
    sensor_name: str | None = None
    sensor_type: str | None = None
    tag: str | None = None
    picket_label: str | None = None
    segment_label: str | None = None
    address_known: bool = True


@dataclass(frozen=True)
class Alert:
    alert_id: str
    case_key: str
    schema_version: str
    # что и по какому направлению ТЗ
    head: str
    direction: str
    direction_title: str
    title: str
    # когда
    asof: dt.date                 # сутки, по данным которых считан риск
    valid_from: dt.datetime       # начало окна, к которому относится прогноз
    valid_to: dt.datetime         # конец окна
    horizon_hours: int
    # насколько
    risk: float                   # калиброванная вероятность события в окне
    rank: int                     # место в суточном списке головы, с единицы
    in_budget: bool               # попал ли в суточный бюджет выездов
    above_threshold: bool | None  # превысил ли порог рабочей точки, если он задан
    # куда
    address: Address
    # состояние
    status: str = STATUS_OK
    status_note: str | None = None
    # чем считано
    model_version: str | None = None
    feature_signature: str | None = None
    factors: list[dict] = field(default_factory=list)
    # Source-aware operational note, separate from model contributions/risk.
    maintenance_context: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["asof"] = self.asof.isoformat()
        d["valid_from"] = self.valid_from.isoformat()
        d["valid_to"] = self.valid_to.isoformat()
        return d


@dataclass(frozen=True)
class Coverage:
    """Покрытие головы: по скольким сущностям она вообще может отвечать.

    Нужно, чтобы интерфейс мог честно показать «по этому объекту прогноз не
    строится», а не оставить пустое место, которое читается как «всё спокойно».
    """
    head: str
    direction: str
    entities_total: int
    entities_scored: int
    reason: str | None = None

    @property
    def fraction(self) -> float:
        return (self.entities_scored / self.entities_total
                if self.entities_total else 0.0)

    def to_dict(self) -> dict:
        return {**asdict(self), "fraction": round(self.fraction, 4)}
