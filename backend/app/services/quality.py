"""Качество прогноза по неделям окна демо.

ЗАГЛУШКА — владелец BE-05 (C2).
Заменить: агрегат по forecasts и outcomes за окно демо (выдано, попало, мимо,
неизвестно, precision по неделям); base_rate и rule_precision — из реестра ML1-08.
Сейчас — 4 синтетические недели перед неделей demo_today, числа детерминированы
сценарием и датой.
Контракт: weekly(db, scenario) -> QualityOut не меняется; тест
tests/test_endpoints_shape.py должен остаться зелёным.
"""
import hashlib
from datetime import timedelta

from sqlalchemy.orm import Session

from ..schemas.misc import QualityOut, QualityWeek
from . import settings_store

WEEKS = 4


def weekly(db: Session, scenario: str) -> QualityOut:
    today = settings_store.demo_today(db)
    this_monday = today - timedelta(days=today.weekday())
    weeks = []
    for k in range(WEEKS, 0, -1):
        start = this_monday - timedelta(weeks=k)
        digest = hashlib.sha256(f"{scenario}|{start.isoformat()}".encode()).digest()
        issued = 10 + digest[0] % 20
        hit = digest[1] % (issued // 2 + 1)
        unknown = digest[2] % 3
        miss = issued - hit - unknown
        weeks.append(QualityWeek(week_start=start, issued=issued, hit=hit, miss=miss,
                                 unknown=unknown,
                                 precision=round(hit / (hit + miss), 3) if hit + miss else None))
    return QualityOut(scenario=scenario, weeks=weeks, base_rate=None, rule_precision=None,
                      note="синтетические данные каркаса, не результат модели", source="stub")
