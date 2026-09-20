"""Сервисный слой: то, что вызывает веб-приложение.

Между моделью и интерфейсом должен стоять слой, который переживает
переобучение. Здесь он: наружу отдаются объекты из mkl.contract, внутрь уходят
все подробности про бэкенды, фичестор и отсечку бюджета.

Функции сознательно синхронные и без веб-зависимостей — REST-обвязка живёт
отдельно и может быть какой угодно. Так же проще тестировать и так же проще
встроить в чужой процесс, если веб-часть решит вызывать напрямую.
"""
import datetime as dt

import polars as pl

from . import contract, explain, serve, store, train
from .contract import Address, Alert, Coverage

# Сколько факторов показывать в карточке алерта.
TOP_FACTORS = 5


def _address(row: dict) -> Address:
    return Address(
        obj=_s(row.get("obj")),
        obj_parent=_s(row.get("obj_parent")),
        obj_kind=_s(row.get("obj_kind")),
        picket=_f(row.get("picket")),
        channel=_i(row.get("ch")),
        sensor_type=_s(row.get("stype")),
        sensor_name=_s(row.get("sname")),
        segment=_i(row.get("seg")),
    )


def _s(v):
    return None if v is None else str(v)


def _f(v):
    return None if v is None else float(v)


def _i(v):
    return None if v is None else int(v)


def alerts_for_head(head: str, asof: dt.date | None = None,
                    with_factors: bool = True) -> list[Alert]:
    """Алерты одной головы за сутки asof в форме контракта."""
    cfg = serve.load_heads()[head]
    df, art, feats = serve.score_with_internals(head, asof)
    if df.is_empty():
        return []

    day = df["day"][0] if "day" in df.columns else (asof or dt.date.today())
    if isinstance(day, dt.datetime):
        day = day.date()
    horizon = int(cfg["horizon_days"]) * 24
    # Окно прогноза начинается в конце суток, по которым посчитаны признаки:
    # метка живёт в (day, day + horizon], и обещать раньше нечего.
    start = dt.datetime.combine(day, dt.time()) + dt.timedelta(days=1)

    factors: list[list[dict]] = []
    if with_factors:
        # Порядок строк матрицы обязан совпадать с порядком алертов. Сортировка
        # признаков по сущности и алертов по риску — разные порядки, и вклады
        # тогда приклеиваются к чужим строкам: числа выглядят осмысленно, а
        # относятся не к тому алерту. Поэтому не сортировка, а join В ПОРЯДКЕ df.
        keys = [c for c in ("ch", "obj", "seg", "day")
                if c in df.columns and c in feats.columns]
        aligned = (df.select(keys).join(feats, on=keys, how="left")
                   if keys else feats)
        if aligned.height != df.height:
            raise ValueError(
                f"{head}: признаки не сошлись с алертами построчно "
                f"({aligned.height} против {df.height}) — вклады признаков "
                f"были бы приклеены к чужим строкам")
        X = train._matrix(aligned, art["features"])
        factors = explain.contributions(art["model"], X, art["features"],
                                        top=TOP_FACTORS)

    rows = df.to_dicts()
    out: list[Alert] = []
    for rank, row in enumerate(rows, start=1):
        ent = {k: row[k] for k in ("ch", "obj", "seg") if k in row}
        out.append(Alert(
            alert_id=contract.make_alert_id(head, ent, day),
            case_key=contract.make_case_key(head, ent),
            schema_version=contract.SCHEMA_VERSION,
            head=head,
            direction=cfg["direction"],
            direction_title=contract.DIRECTIONS.get(cfg["direction"], cfg["direction"]),
            title=cfg["title"],
            asof=day,
            valid_from=start,
            valid_to=start + dt.timedelta(hours=horizon),
            horizon_hours=horizon,
            risk=float(row["risk"]),
            rank=rank,
            in_budget=bool(row.get("alert", False)),
            above_threshold=row.get("above_thr"),
            address=_address(row),
            model_version=art.get("saved_at"),
            feature_signature=art.get("feature_signature"),
            factors=factors[rank - 1] if rank - 1 < len(factors) else [],
        ))
    return out


def daily_alerts(asof: dt.date | None = None, heads: list[str] | None = None,
                 only_in_budget: bool = True,
                 with_factors: bool = True) -> list[Alert]:
    """Выдача за сутки по всем головам, у которых обучена модель.

    only_in_budget отдаёт лишь то, что сервис действительно предлагает к
    выезду. Полный список с рангами нужен для журнала прогнозов, поэтому
    отключаемо, а не зашито.
    """
    names = heads or [h for h in serve.load_heads()
                      if serve.model_path(h).exists()]
    out: list[Alert] = []
    for head in names:
        try:
            got = alerts_for_head(head, asof, with_factors=with_factors)
        except (ValueError, FileNotFoundError) as exc:
            print(f"голова {head} пропущена: {exc}", flush=True)
            continue
        out.extend(a for a in got if a.in_budget or not only_in_budget)
    out.sort(key=lambda a: (-a.risk, a.head))
    return out


def coverage(asof: dt.date | None = None) -> list[Coverage]:
    """По скольким сущностям голова может отвечать.

    Голова несанкционированного доступа работает только там, где есть события
    постановки на охрану: 47 объектов из 78. Пустое место в интерфейсе по
    остальным читалось бы как «всё спокойно», что неправда.
    """
    out: list[Coverage] = []
    for head, cfg in serve.load_heads().items():
        if not serve.model_path(head).exists():
            continue
        feats = (store.latest_snapshot(cfg["feature_set"]) if asof is None
                 else store.read_slice(cfg["feature_set"], asof, asof))
        ent = next((k for k in ("ch", "obj", "seg") if k in feats.columns), None)
        total = feats[ent].n_unique() if ent else feats.height
        try:
            scored = len({a.address.obj or a.address.channel
                          for a in alerts_for_head(head, asof, with_factors=False)})
        except (ValueError, FileNotFoundError):
            scored = 0
        reason = None
        if cfg.get("armed_only"):
            reason = ("считается только по объектам с данными о постановке на "
                      "охрану: 47 из 78")
        out.append(Coverage(head=head, direction=cfg["direction"],
                            entities_total=int(total), entities_scored=int(scored),
                            reason=reason))
    return out
