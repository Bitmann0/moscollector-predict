"""Бандл C4: сборка scripts/build_bundle.py и проверка скриптами fetch_bundle.

Корень данных поддельный и крошечный: сборщик содержимое не разбирает, кроме
JSON v2-кэша, поэтому хватает файлов по нескольку байт. Скрипты fetch_bundle
лежат в корне монорепозитория; где их нет (образ ML) или нет sh, PowerShell,
7z, тесты на них пропускаются.
"""
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import build_bundle as bb

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
FETCH_SH = REPO / "scripts" / "fetch_bundle.sh"
FETCH_PS1 = REPO / "scripts" / "fetch_bundle.ps1"
VERSION = "bundle-20260928-1"
# Файлы корня, которых в бандле быть не должно.
EXTRA = ("data/interim/events_year=2025.parquet", "data/features/segment.parquet",
         "data/interim/weather.parquet", "data/raw/ext-journal-2026.csv",
         "models/B.pkl", "models/D.pkl", "models/D@2026-05-31.pkl",
         "reports/final.md", "configs/heads.yaml")


def make_root(root: Path) -> Path:
    for rel in (*bb.REQUIRED, *EXTRA):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"fake {rel}\n".encode())
    (root / "reports/intrusion_eventtime_v2_build.json").write_text(
        json.dumps({"version": 2, "start": "2023-01-01", "end": "2026-06-29"}),
        encoding="utf-8")
    return root


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path / "src")


@pytest.fixture
def staging(root, tmp_path):
    path, _, _ = bb.build(root, tmp_path / "out", VERSION)
    return path


def files_under(path: Path) -> set[str]:
    return {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}


def manifest(path: Path) -> list[tuple[str, str]]:
    text = (path / bb.MANIFEST).read_bytes().decode("utf-8")
    assert "\r" not in text, "sha256sum -c на Linux не примет CRLF"
    rows = []
    for line in text.splitlines():
        m = re.fullmatch(r"([0-9a-f]{64})  (\S.*)", line)
        assert m, f"строка не в формате sha256sum: {line!r}"
        rows.append((m.group(1), m.group(2)))
    return rows


def test_bundle_holds_required_files_and_nothing_else(staging):
    assert files_under(staging) == set(bb.REQUIRED) | {bb.MANIFEST}


def test_manifest_lists_every_file_with_its_source_hash(root, staging):
    rows = manifest(staging)
    assert [rel for _, rel in rows] == sorted(bb.REQUIRED)
    for digest, rel in rows:
        assert digest == hashlib.sha256((root / rel).read_bytes()).hexdigest()
        assert (staging / rel).read_bytes() == (root / rel).read_bytes()


def test_layout_is_the_compose_contract(staging):
    assert bb.LAYOUT == ("data", "models", "configs/features.yaml",
                         "reports/intrusion_eventtime_v2_build.json")
    assert (staging / "data").is_dir() and (staging / "models").is_dir()
    assert (staging / "configs/features.yaml").is_file()
    assert (staging / "reports/intrusion_eventtime_v2_build.json").is_file()
    # Materials монтирует не ml, а api (справочник BE-03), поэтому в LAYOUT его нет.
    assert {rel.split("/")[0] for rel in bb.REQUIRED} == {"data", "models", "configs",
                                                          "reports", "Materials"}


def test_missing_required_files_are_named_together_before_copying(root, tmp_path):
    (root / "data/interim/episodes.parquet").unlink()
    (root / "models/A_link.pkl").unlink()
    with pytest.raises(bb.BundleError) as err:
        bb.build(root, tmp_path / "out", VERSION)
    assert "data/interim/episodes.parquet" in str(err.value)
    assert "models/A_link.pkl" in str(err.value)
    assert not (tmp_path / "out").exists()


def test_optional_files_and_window_models_are_taken_when_present(root, tmp_path):
    for rel in ("data/interim/maintenance_2026.json", "models/A_link@2026-05-31.pkl",
                "models/A_link@2026-06-14.pkl"):
        (root / rel).write_bytes(b"{}")
    staging, _, absent = bb.build(root, tmp_path / "out", VERSION)
    got = files_under(staging)
    assert {"data/interim/maintenance_2026.json", "models/A_link@2026-05-31.pkl",
            "models/A_link@2026-06-14.pkl"} <= got
    assert "models/D@2026-05-31.pkl" not in got
    assert absent == []
    assert {rel for _, rel in manifest(staging)} == got - {bb.MANIFEST}


def test_absent_optional_file_is_reported_not_fatal(root, tmp_path):
    _, _, absent = bb.build(root, tmp_path / "out", VERSION)
    assert absent == ["data/interim/maintenance_2026.json"]


@pytest.mark.parametrize("payload", ['{"version": 1, "end": "2026-06-29"}',
                                     '{"version": 2}', "not json"])
def test_guard_cache_must_be_version_2(root, tmp_path, payload):
    (root / "reports/intrusion_eventtime_v2_build.json").write_text(payload,
                                                                   encoding="utf-8")
    with pytest.raises(bb.BundleError, match="intrusion_eventtime_v2_build.json"):
        bb.build(root, tmp_path / "out", VERSION)


def test_refuses_bad_version_and_non_empty_staging(root, tmp_path):
    with pytest.raises(bb.BundleError, match="bundle-YYYYMMDD-N"):
        bb.build(root, tmp_path / "out", "bundle-2026-09-28")
    bb.build(root, tmp_path / "out", VERSION)
    with pytest.raises(bb.BundleError, match="не пуст"):
        bb.build(root, tmp_path / "out", VERSION)


def test_dry_run_copies_nothing(root, tmp_path, capsys):
    rc = bb.main(["--source", str(root), "--out", str(tmp_path / "out"), "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "out").exists()
    assert f"итого, файлов {len(bb.REQUIRED)}" in capsys.readouterr().out


def test_cli_exit_code_on_missing_file(root, tmp_path, capsys):
    (root / "configs/features.yaml").unlink()
    rc = bb.main(["--source", str(root), "--out", str(tmp_path / "out"),
                  "--version", VERSION])
    assert rc == 2
    assert "configs/features.yaml" in capsys.readouterr().err


def test_printed_7z_command_never_shows_password():
    cmd = bb.archive_command("7z", Path("b.7z"), 1, "s3cret-value")
    assert "-ps3cret-value" in cmd
    assert "s3cret" not in bb.masked(cmd)
    # Без переменной — голый -p: 7z спросит пароль с клавиатуры.
    assert "-p" in bb.archive_command("7z", Path("b.7z"), 1, None)


def test_bundle_paths_match_service_code():
    """Пути сборщика — те же, что читает mkl; разойдутся — упадёт здесь, а не в /ready."""
    from mkl import guard_queue, serve, store
    from mkl.config import PATHS

    def rel(path: Path) -> str:
        return path.relative_to(PATHS.root).as_posix()

    assert rel(store.REGISTRY) in bb.REQUIRED
    assert rel(guard_queue.EVENT_DAYS) in bb.REQUIRED
    assert rel(guard_queue.BUILD_INFO) in bb.REQUIRED
    for head in bb.MODEL_HEADS:
        assert rel(serve.model_path(head)) in bb.REQUIRED
    for name in ("sensor", "object"):
        assert rel(PATHS.features / f"{name}.parquet") in bb.REQUIRED
    for name in ("channels", "daily_channel", "episodes"):
        assert rel(PATHS.interim / f"{name}.parquet") in bb.REQUIRED


def test_model_heads_are_pilot_heads_with_a_model_file():
    heads = yaml.safe_load((ML / "configs" / "heads.yaml").read_text(encoding="utf-8"))
    pilots = {h for h, cfg in heads.items() if cfg.get("product_status") == "pilot"}
    with_model = {h for h in pilots if not heads[h].get("serving_rule")}
    assert set(bb.MODEL_HEADS) == with_model


def test_compose_real_mounts_come_from_bundle_layout():
    compose = REPO / "compose.real.yaml"
    if not compose.exists():
        pytest.skip("compose.real.yaml есть только в монорепозитории")
    mounts = re.findall(r"\$\{BUNDLE_DIR:-\./bundle\}/(\S+):/srv/ml/(\S+)",
                        compose.read_text(encoding="utf-8"))
    assert mounts, "в compose.real.yaml не нашлось томов бандла"
    assert {src for src, _ in mounts} == set(bb.LAYOUT)
    assert all(src == dst for src, dst in mounts)


# --- fetch_bundle.sh и .ps1 на собранном бандле --------------------------------

def run(cmd: list[str], env: dict | None = None, cwd: Path | None = None):
    full = {**os.environ, **(env or {})}
    full.pop("BUNDLE_URL", None)
    if env is None or "BUNDLE_PASSWORD" not in env:
        full.pop("BUNDLE_PASSWORD", None)
    done = subprocess.run(cmd, capture_output=True, env=full, cwd=cwd, timeout=300, check=False,
                          stdin=subprocess.DEVNULL)
    out = (done.stdout + done.stderr).decode("utf-8", errors="replace")
    return done.returncode, out


def bundle_dir_line(out: str) -> Path:
    lines = [line for line in out.splitlines() if line.startswith("BUNDLE_DIR=")]
    assert len(lines) == 1, out
    return Path(lines[0].split("=", 1)[1].strip())


def sh_cmd() -> list[str]:
    sh = shutil.which("sh")
    if not FETCH_SH.exists() or not sh:
        pytest.skip("нет scripts/fetch_bundle.sh или sh")
    if not (shutil.which("sha256sum") or shutil.which("shasum")):
        pytest.skip("нет sha256sum и shasum")
    return [sh, FETCH_SH.as_posix()]


def ps_cmd(name: str) -> list[str]:
    exe = shutil.which(name)
    if not FETCH_PS1.exists() or not exe:
        pytest.skip(f"нет scripts/fetch_bundle.ps1 или {name}")
    return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(FETCH_PS1)]


def fetchers() -> list:
    return [pytest.param(sh_cmd, id="sh"),
            pytest.param(lambda: ps_cmd("pwsh"), id="pwsh"),
            pytest.param(lambda: ps_cmd("powershell"), id="powershell")]


@pytest.mark.parametrize("fetch", fetchers())
def test_fetch_verifies_unpacked_bundle(fetch, staging):
    rc, out = run(fetch() + [staging.as_posix()])
    assert rc == 0, out
    assert bundle_dir_line(out).samefile(staging)


@pytest.mark.parametrize("fetch", fetchers())
def test_fetch_rejects_corrupted_file(fetch, staging):
    (staging / "data/interim/channels.parquet").write_bytes(b"changed")
    rc, out = run(fetch() + [staging.as_posix()])
    assert rc == 1, out
    assert "channels.parquet: FAILED" in out
    assert "BUNDLE_DIR=" not in out


@pytest.mark.parametrize("fetch", fetchers())
def test_fetch_rejects_broken_layout(fetch, staging):
    (staging / "configs/features.yaml").unlink()
    rc, out = run(fetch() + [staging.as_posix()])
    assert rc == 1, out
    assert "features.yaml" in out
    assert "BUNDLE_DIR=" not in out


@pytest.mark.parametrize("fetch", fetchers())
def test_archive_round_trip_under_password(fetch, root, tmp_path, capfd, monkeypatch):
    """build --archive → fetch из .7z: пароль из окружения, в выводе его нет."""
    cmd = fetch()
    if not any(shutil.which(name) for name in bb.SEVEN_ZIP):
        pytest.skip("нет 7z")
    password = "test-" + secrets.token_hex(8)
    out_dir = tmp_path / "out"
    monkeypatch.setenv("BUNDLE_PASSWORD", password)
    rc = bb.main(["--source", str(root), "--out", str(out_dir), "--version", VERSION,
                  "--archive"])
    monkeypatch.delenv("BUNDLE_PASSWORD")
    built = capfd.readouterr()
    assert rc == 0, built.err
    assert password not in built.out + built.err
    archive = out_dir / f"{VERSION}.7z"
    assert archive.is_file()

    dest = tmp_path / "unpacked" / "bundle"
    rc, out = run(cmd + [archive.as_posix(), dest.as_posix()],
                  env={"BUNDLE_PASSWORD": "wrong-" + password})
    assert rc == 1, out
    assert not dest.exists()

    rc, out = run(cmd + [archive.as_posix(), dest.as_posix()],
                  env={"BUNDLE_PASSWORD": password})
    assert rc == 0, out
    assert password not in out
    assert bundle_dir_line(out).samefile(dest)
    assert files_under(dest) == files_under(out_dir / VERSION)
    assert not Path(f"{dest}.partial").exists()

    # Повторно в тот же каталог — отказ, готовый бандл не трогаем.
    rc, out = run(cmd + [archive.as_posix(), dest.as_posix()],
                  env={"BUNDLE_PASSWORD": password})
    assert rc == 1, out
    assert files_under(dest) == files_under(out_dir / VERSION)


@pytest.mark.parametrize("fetch", fetchers())
def test_fetch_finds_version_archive_in_current_dir(fetch, staging, tmp_path):
    cmd = fetch()
    seven_zip = next((shutil.which(n) for n in bb.SEVEN_ZIP if shutil.which(n)), None)
    if not seven_zip:
        pytest.skip("нет 7z")
    work = tmp_path / "work"
    work.mkdir()
    password = "test-" + secrets.token_hex(8)
    subprocess.run(bb.archive_command(seven_zip, work / f"{VERSION}.7z", 1, password),
                   cwd=staging, check=True, capture_output=True)
    rc, out = run(cmd + [VERSION, "bundle"], env={"BUNDLE_PASSWORD": password}, cwd=work)
    assert rc == 0, out
    assert bundle_dir_line(out).samefile(work / "bundle")


@pytest.mark.parametrize("fetch", fetchers())
def test_fetch_without_source_is_usage_error(fetch):
    rc, _ = run(fetch())
    assert rc == 2
