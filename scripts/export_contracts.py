"""Пересобирает содержимое contracts/ из кода. Живое.

    python scripts/export_contracts.py            # схемы (и фикстуры, когда готовы заглушки)
    python scripts/export_contracts.py --schemas-only

CI запускает скрипт и падает, если после него `git diff contracts/` не пуст:
поменял схему — перегенерируй и закоммить, потребитель увидит изменение в PR.

Задача 5 плана каркаса дописывает сюда выгрузку фикстур (раздел «Контракты»
спецификации): ML — из mkl.product_stub, backend — ответы заглушек на тестовой БД.
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
sys.path.insert(0, str(ROOT / "backend"))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def export_openapi() -> None:
    from app.main import create_app
    write_json(CONTRACTS / "api_v1.openapi.json", create_app().openapi())


def export_ml_schema() -> None:
    spec = importlib.util.spec_from_file_location(
        "ml_product_contract", ROOT / "ml" / "src" / "mkl" / "product_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    models = {name: obj for name, obj in vars(module).items()
              if isinstance(obj, type) and hasattr(obj, "model_json_schema")
              and obj.__module__ == module.__name__}
    from pydantic.json_schema import models_json_schema
    _, schema = models_json_schema([(m, "validation") for m in models.values()],
                                   title="Контракт C1: ML → backend")
    schema["schema_version"] = module.SCHEMA_VERSION
    write_json(CONTRACTS / "ml_v1.schema.json", schema)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schemas-only", action="store_true")
    args = parser.parse_args()
    export_openapi()
    export_ml_schema()
    if not args.schemas_only:
        print("фикстуры: выгрузку дописывает задача 5 плана каркаса", file=sys.stderr)


if __name__ == "__main__":
    main()
