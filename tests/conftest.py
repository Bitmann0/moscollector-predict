from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def sample_data_dir(tmp_path: Path) -> Path:
    """Создаёт автономную обезличенную выгрузку для API-тестов."""
    data_dir = tmp_path / "raw"
    data_dir.mkdir()
    channels = pd.DataFrame(
        [
            {
                "ид_канала_данных": 10,
                "тип_инж_системы": "Температурная подсистема",
                "тип_датчика": "Датчик температуры",
                "тег_инженерной_системы": "15-1.2",
                "название_датчика": "Температура ВШ",
            },
            {
                "ид_канала_данных": 20,
                "тип_инж_системы": "Охранная подсистема",
                "тип_датчика": "КД Дверь",
                "тег_инженерной_системы": "15-1.3",
                "название_датчика": "Дверь ПК1",
            },
        ]
    )
    events = []
    for index in range(12):
        timestamp = pd.Timestamp("2026-08-01 10:00:00") + pd.Timedelta(index * 10, unit="s")
        events.append(
            {
                "ид_события": index + 1,
                "ид_канала_данных": 10,
                "дата": timestamp.date().isoformat(),
                "время": timestamp.time().isoformat(),
                "тревожное": index < 4,
                "значение_датчика": "42",
            }
        )
    for index in range(3):
        timestamp = pd.Timestamp("2026-08-01 10:00:00") + pd.Timedelta(index, unit="h")
        events.append(
            {
                "ид_события": 100 + index,
                "ид_канала_данных": 20,
                "дата": timestamp.date().isoformat(),
                "время": timestamp.time().isoformat(),
                "тревожное": False,
                "значение_датчика": str(index % 2),
            }
        )
    channels.to_csv(data_dir / "справочник_каналов_датчиков.csv", index=False)
    pd.DataFrame(events).to_csv(data_dir / "журнал_событий_пример.csv", index=False)
    return data_dir
