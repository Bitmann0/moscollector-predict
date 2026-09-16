"""Exercise first-start volume permissions and historical readiness with synthetic data."""

import csv
import http.client
import json
import pathlib
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid


def main() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    project = "moscollector-smoke-" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as temporary:
        work = pathlib.Path(temporary)
        work.chmod(0o755)
        raw = work / "raw"
        raw.mkdir(mode=0o755)
        tables = {
            "справочник_каналов_датчиков.csv": [
                [
                    "ид_канала_данных",
                    "тип_инж_системы",
                    "тип_датчика",
                    "тег_инженерной_системы",
                    "название_датчика",
                ],
                [10, "Охранная подсистема", "КД Дверь", "demo-1", "Учебный канал"],
            ],
            "журнал_событий_пример.csv": [
                [
                    "ид_события",
                    "ид_канала_данных",
                    "дата",
                    "время",
                    "тревожное",
                    "значение_датчика",
                ],
                [1, 10, "2020-01-01", "10:00:00", "f", "Норма"],
                [2, 10, "2020-01-01", "10:00:10", "t", "Неисправен"],
            ],
        }
        for name, rows in tables.items():
            with (raw / name).open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream).writerows(rows)
        override = work / "compose.json"
        override.write_text(
            json.dumps(
                {
                    "services": {
                        "api": {
                            "image": "moscollector-predict:test",
                            "volumes": [f"{raw.as_posix()}:/app/data/raw:ro"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(root / "compose.yaml"),
            "-f",
            str(override),
        ]
        try:
            subprocess.run([*compose, "up", "-d", "--no-build"], check=True)
            for attempt in range(45):
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:8000/api/v1/ready", timeout=2
                    ) as r:
                        state = json.load(r)
                    assert state["ready"] and state["mode"] == "historical"
                    break
                except (OSError, urllib.error.URLError, http.client.HTTPException):
                    if attempt == 44:
                        raise
                    time.sleep(1)
            request = urllib.request.Request(
                "http://127.0.0.1:8000/api/v1/maintenance-requests",
                data=b'{"channel_id":10}',
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                draft = json.load(response)
            assert draft["created"] and draft["item"]["assessment_mode"] == "historical"
            assert draft["item"]["data_as_of"] == "2020-01-01T07:00:10+00:00"
            print("Compose started with a fresh volume; readiness and persisted draft verified.")
        finally:
            subprocess.run([*compose, "logs", "--tail", "50"], check=False)
            subprocess.run([*compose, "down", "--volumes"], check=True)


if __name__ == "__main__":
    main()
