from __future__ import annotations

import argparse
from pathlib import Path

from .model import analyze


def main() -> None:
    parser = argparse.ArgumentParser(description="Анализ выгрузки СМВУ")
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    forecasts, metadata = analyze(args.data_dir)
    print(f"Событий: {metadata['event_count']}; активных каналов: {metadata['channel_count']}")
    print("Топ каналов по риску:")
    for item in forecasts[: args.top]:
        print(
            f"  {item.channel_id:<8} {item.risk_score:>6.1%} "
            f"{item.risk_level:<8} {item.sensor_name}"
        )


if __name__ == "__main__":
    main()

