import datetime as dt
import hashlib
import os
import pickle
import tempfile
from pathlib import Path

import polars as pl
import yaml

from . import store
from .config import EQUIPMENT_STYPES, PATHS

HEADS_CONFIG = PATHS.root / "configs" / "heads.yaml"


def load_heads() -> dict:
    return yaml.safe_load(HEADS_CONFIG.read_text(encoding="utf-8"))


def model_path(head: str) -> Path:
    return PATHS.models / f"{head}.pkl"


def feature_signature(name: str) -> str:
    """Отпечаток набора признаков: имена и типы, отсортированные.

    Связывает модель с тем фичестором, на котором она обучена. Дата сборки для
    этого не годится: пересборка того же кода меняет дату и не меняет смысла,
    а переименование колонки меняет смысл и может не поменять дату.
    """
    reg = store.load_registry().get(name, {})
    dtypes = reg.get("dtypes") or {}
    payload = ";".join(f"{k}:{dtypes[k]}" for k in sorted(dtypes))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def save(head: str, model, iso, feature_names: list[str],
         threshold: float | None = None,
         metadata: dict | None = None) -> Path:
    PATHS.models.mkdir(parents=True, exist_ok=True)
    registry = store.load_registry()
    cfg = load_heads()[head]
    dst = model_path(head)
    artifact = {
        "model": model,
        "iso": iso,
        "features": feature_names,
        # Порог рабочей точки — часть модели, а не отчёта. Пока он не
        # сохранялся, заявленная в отчёте точка в проде была недостижима:
        # serve резал по бюджету и выдавал другой список.
        "threshold": threshold,
        "feature_set": cfg["feature_set"],
        "feature_signature": feature_signature(cfg["feature_set"]),
        "feature_set_built_at": {k: v.get("built_at") for k, v in registry.items()},
        "saved_at": dt.datetime.now().isoformat(timespec="seconds"),
        "metadata": metadata or {},
    }
    # В живом сервисе старый артефакт должен оставаться читаемым до завершения
    # нового обучения: прерванная запись прямо в .pkl оставляла битую модель.
    fd, temp_name = tempfile.mkstemp(prefix=f".{head}.", suffix=".tmp",
                                      dir=PATHS.models)
    try:
        with os.fdopen(fd, "wb") as f:
            pickle.dump(artifact, f)
        os.replace(temp_name, dst)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return dst


_with_internals = False


def _apply_budget(df: pl.DataFrame, budget: int,
                  per_object: bool = False) -> pl.DataFrame:
    """Отсечка по бюджету алертов.

    per_object раздаёт бюджет внутри объектов, а не глобально: при глобальной
    отсечке все алерты оседают на нескольких худших объектах, и остальные
    диспетчеры сервисом просто не пользуются.
    """
    if per_object and "obj" not in df.columns:
        # Прежде здесь был молчаливый откат к глобальной отсечке, и три головы
        # из восьми работали в режиме, который сами в конфигурации называют
        # неприемлемым. Отсутствие obj — это ошибка сборки, а не повод
        # незаметно поменять политику.
        raise ValueError("побъектный бюджет запрошен, а колонки obj нет")
    if df.is_empty():
        return df.with_columns(pl.lit(False).alias("alert"))
    if per_object:
        # Раздача по кругу: сперва по одному лучшему с каждого объекта, затем по
        # второму и так далее, пока не кончится бюджет. Порядок внутри круга —
        # по риску.
        #
        # Прежняя формула max(1, бюджет // объектов) при бюджете меньше числа
        # объектов давала по одному каждому и выдавала БОЛЬШЕ бюджета: десять
        # объектов при бюджете пять давали десять алертов. Это тот же дефект,
        # что был у глобальной отсечки, только незамеченный: обещание «не больше
        # N выездов в сутки» нарушалось вдвое.
        tie = [c for c in ("ch", "seg", "day") if c in df.columns]
        k = min(int(budget), df.height)
        return (df.sort(["obj", "risk"] + tie,
                        descending=[False, True] + [False] * len(tie))
                  .with_columns(pl.col("obj").cum_count().over("obj")
                                .alias("_in_obj"))
                  .sort(["_in_obj", "risk", "obj"] + tie,
                        descending=[False, True] + [False] * (len(tie) + 1))
                  .with_row_index("_rank")
                  .with_columns((pl.col("_rank") < k).alias("alert"))
                  .drop("_rank", "_in_obj"))
    # Ровно k алертов, а не «все, кто не ниже k-го». На ступенчатом выходе
    # изотоники пороговое значение делят десятки строк, и обещание «не больше
    # 20 выездов в сутки» нарушалось до 1.92 раза. Ничьи разрываются ключами
    # сущности, иначе состав списка зависит от порядка строк.
    tie = [c for c in ("obj", "ch", "seg", "day") if c in df.columns]
    k = min(int(budget), df.height)
    return (df.sort(["risk"] + tie, descending=[True] + [False] * len(tie))
              .with_row_index("_rank")
              .with_columns((pl.col("_rank") < k).alias("alert"))
              .drop("_rank"))


def score(head: str, asof: dt.date | None = None) -> pl.DataFrame:
    """Скоринг одной головы. Фичи берутся только из фичестора,
    построенного той же функцией compute.build_all, что и на обучении."""
    cfg = load_heads()[head]
    feats = (store.latest_snapshot(cfg["feature_set"]) if asof is None
             else store.read_slice(cfg["feature_set"], asof, asof))
    if feats.is_empty():
        raise ValueError(f"{head}: no feature rows for requested day {asof}")
    if cfg.get("serving_rule"):
        # Голова-правило (reports/RULE_VS_MODEL_RESULT.md): порог выбирается к
        # дню расчёта, файла модели нет.
        from . import rule_head
        day = asof or feats["day"].max()
        art = rule_head.artifact(head, cfg, day)
    else:
        with model_path(head).open("rb") as f:
            art = pickle.load(f)
    if head == "D":
        # The wear target is defined only for equipment sensor types. Scoring
        # every channel and then taking top-3 lets out-of-scope sensors consume
        # the entire dispatch budget despite having no defined target.
        if "stype" not in feats.columns:
            raise ValueError("D requires stype to match its target population")
        feats = feats.filter(pl.col("stype").is_in(EQUIPMENT_STYPES))
    # Порядок и состав колонок берутся из артефакта модели, а не из фичестора:
    # так лишний признак, добавленный позже, не сдвинет вектор на инференсе.
    missing = [c for c in art["features"] if c not in feats.columns]
    if missing:
        raise ValueError(
            f"фичестор не содержит признаков модели {head}: {missing[:5]}"
        )

    sig = art.get("feature_signature")
    if sig and sig != feature_signature(cfg["feature_set"]):
        raise ValueError(
            f"модель {head} обучена на другом наборе признаков "
            f"({cfg['feature_set']}): отпечаток {sig} против "
            f"{feature_signature(cfg['feature_set'])}. Переобучите голову или "
            f"верните прежний фичестор — молча подставлять другие числа в те "
            f"же слоты нельзя."
        )

    # Голова не должна высказываться о сущностях, которых не видела при
    # обучении. Метка несанкционированного доступа определена только там, где
    # известно состояние охраны; на остальных объектах модель выдавала бы риск,
    # обученный на другом определении события. Это хуже молчания.
    if cfg.get("armed_only"):
        if "obj_armed" not in feats.columns:
            raise ValueError(
                f"голова {head} требует состояния охраны, но в наборе "
                f"{cfg['feature_set']} нет колонки obj_armed")
        feats = feats.filter(pl.col("obj_armed").is_not_null())

    per_object = bool(cfg.get("budget_per_object"))
    if per_object and "obj" not in feats.columns:
        raise ValueError(
            f"голова {head} требует побъектного бюджета, но в наборе "
            f"{cfg['feature_set']} нет колонки obj"
        )
    # Объект и пикет остаются в выдаче даже там, где сущность — канал:
    # алерт без адреса диспетчеру бесполезен, а побъектный бюджет без obj
    # молча вырождался в глобальный.
    keys = [k for k in cfg["entity"] if k in feats.columns]
    keys += [c for c in ("obj", "obj_parent", "picket", "stype")
             if c in feats.columns and c not in keys]
    if art.get("rule"):
        # Риск правила — значение признака, а не вероятность.
        risk = (feats[art["rule"]].cast(pl.Float64).fill_nan(0.0).fill_null(0.0)
                .to_numpy())
    else:
        risk = art["model"].predict_proba(feats.select(art["features"]).to_numpy())[:, 1]
        if art["iso"] is not None:
            from .calibrate import apply as cal_apply
            risk = cal_apply(art["iso"], risk)

    out = feats.select(keys).with_columns(pl.Series("risk", risk))
    if art.get("threshold") is not None:
        out = out.with_columns((pl.col("risk") >= art["threshold"]).alias("above_thr"))
    out = _apply_budget(out, cfg["budget_per_day"], per_object=per_object
                        ).sort("risk", descending=True)
    if "above_thr" in out.columns:
        # Порог выбран на валидации. Иначе сохранённый порог лишь отображается
        # в ответе, а диспетчеру всё равно отправляются k алертов каждый день.
        out = out.with_columns((pl.col("alert") & pl.col("above_thr")).alias("alert"))
    return (out, art, feats) if _with_internals else out


def score_with_internals(head: str, asof: dt.date | None = None):
    """То же, что score, но отдаёт ещё артефакт и исходные признаки.

    Нужно сервисному слою: вклады признаков в конкретный алерт считаются по той
    же матрице, на которой получен риск, и загружать её второй раз незачем.
    """
    global _with_internals
    _with_internals = True
    try:
        return score(head, asof)
    finally:
        _with_internals = False


def score_all(asof: dt.date | None = None) -> dict[str, pl.DataFrame]:
    return {h: score(h, asof) for h in load_heads() if model_path(h).exists()}


def validate_pilot_artifact(head: str, artifact: dict, day: dt.date,
                            max_lag_days: int = 14) -> None:
    """Reject a missing or future-trained model before issuing a recommendation.

    The threshold window must be past and fully observed at `day`. A weekly
    refresh gives D seven days for label maturation and seven more for the next
    run. Beyond that, scoring can proceed for research but not for the pilot.
    """
    meta = artifact.get("metadata") or {}
    if meta.get("head") != head or "threshold_end" not in meta:
        raise ValueError(f"{head}: pilot model lacks dated training metadata; refresh it")
    cfg = load_heads()[head]
    if meta.get("label") != cfg["label"] or \
            meta.get("horizon_days") != cfg["horizon_days"] or \
            meta.get("variant") != cfg.get("variant") or \
            meta.get("unknown_in_budget") != bool(cfg.get("unknown_in_budget")) or \
            meta.get("operating_min_precision") != float(
                cfg.get("operating_min_precision", 0.7)):
        raise ValueError(f"{head}: pilot target changed since training; refresh it")
    threshold_end = dt.date.fromisoformat(meta["threshold_end"])
    lag = (day - threshold_end).days
    if not 0 < lag <= max_lag_days:
        raise ValueError(
            f"{head}: model threshold window ends {threshold_end}, "
            f"outside the 1..{max_lag_days} day pilot lag for {day}")
    if artifact.get("threshold") is None:
        raise ValueError(f"{head}: pilot model has no saved operating threshold")


def apply_issued_cooldown(ranked: pl.DataFrame, entity: str, day: dt.date,
                          issued: list[tuple[object, dt.date]],
                          cooldown_days: int = 7) -> pl.DataFrame:
    """Suppress already issued cases after ranking, without filling freed slots.

    `issued` is the backend's durable journal of *sent* recommendations, not
    historical high scores. Same-day reruns ignore today's entry and therefore
    produce the same selection. Calendar dates matter when telemetry skips a day.
    """
    if entity not in ranked.columns or "alert" not in ranked.columns:
        raise ValueError("cooldown requires entity and alert columns")
    blocked = {key for key, sent_day in issued
               if 0 < (day - sent_day).days <= cooldown_days}
    return ranked.with_columns(
        (pl.col("alert") & ~pl.col(entity).is_in(list(blocked))).alias("alert"))


def alerts_over_time(df: pl.DataFrame, budget_per_day: int, entity: str,
                     cooldown_days: int = 0, confirm_of_3: bool = False,
                     per_object: bool = False,
                     threshold: float | None = None) -> pl.DataFrame:
    """Суточная выдача с памятью о предыдущих сутках.

    Отсечка по бюджету сама по себе состояния не имеет, и канал, лежащий месяц,
    занимает место в бюджете каждые сутки. По нашей же эпизодной постановке это
    одно событие, которое надо предсказать один раз: тридцать выездов к одному и
    тому же отказу — не тридцать пойманных отказов, а один пойманный и двадцать
    девять потраченных впустую.

    cooldown_days — сколько суток сущность не может попасть в выдачу повторно.
    confirm_of_3 — требовать, чтобы сущность была в верхушке хотя бы дважды за
    последние трое суток: одиночный всплеск риска чаще шум, чем начало отказа.

    Обе настройки тратят полноту ради того, чтобы выданные алерты указывали на
    РАЗНЫЕ события. Цена и выигрыш меряются episodes_per_100_alerts.
    """
    if df.is_empty():
        return df.with_columns(pl.lit(False).alias("alert"))
    days = sorted(df["day"].unique().to_list())
    issued: list[tuple[object, dt.date]] = []
    recent_top: dict = {}
    out = []
    for d in days:
        cur = df.filter(pl.col("day") == d)
        ranked = _apply_budget(cur, budget_per_day, per_object=per_object)
        if threshold is not None:
            ranked = ranked.with_columns(
                (pl.col("alert") & (pl.col("risk") >= threshold)).alias("alert"))
        top = ranked.filter(pl.col("alert"))[entity].to_list()
        for e in top:
            recent_top.setdefault(e, []).append(d)
        if confirm_of_3:
            confirmed = [e for e in top
                         if sum(0 <= (d - seen).days <= 2
                                for seen in recent_top[e]) >= 2]
            ranked = ranked.with_columns(
                (pl.col("alert") & pl.col(entity).is_in(confirmed)).alias("alert"))
        selected = apply_issued_cooldown(
            ranked, entity, d, issued, cooldown_days=cooldown_days)
        issued.extend((e, d) for e in
                      selected.filter(pl.col("alert"))[entity].to_list())
        out.append(selected)
    return pl.concat(out)
