"""Человеческий адрес алерта.

Диспетчер работает названиями, а не идентификаторами: «объект Фита, ПК 28,
КД АВ», а не «объект 3215». Названия живут в справочниках и приклеиваются здесь,
на границе контракта.

Почему не в фичесторе. Строки высокой кардинальности в наборе на 4,27 млн строк
стоят места и времени, меняют отпечаток набора признаков и требуют
переобучения — ради колонок, которые модель видеть не должна. Идентификатор
объекта как признак это к тому же прямая утечка: модель выучит, что «объект
3215 обычно падает», вместо того чтобы смотреть на его состояние. Адрес —
оформление выдачи, и место ему на выходе.

Отдельная забота — честность незнания. Пикета нет у 847 каналов из 11 485, а
участок у головы пожарного риска считается как floor(пикет/10) с подстановкой
нуля вместо пропуска, отчего «участок ПК 0-10» и «пикет неизвестен»
сливаются: из 994 936 строк с участком 0 у 809 934 пикета нет вовсе. Рисовать
такую отметку в начале коллектора значит врать в большинстве случаев, поэтому
здесь она помечается явно.
"""
import functools

import polars as pl

from .config import PATHS

# Вид объекта в справочнике — английский перечислитель.
OBJ_KIND_RU = {
    "controlHouse": "диспетчерский пункт",
    "guardObject": "охранная зона",
}

SEG_SIZE = 10.0
UNKNOWN = "адрес неизвестен"
UNKNOWN_PICKET = "пикет неизвестен"
AMBIGUOUS_SEGMENT = "участок неизвестен"


@functools.lru_cache(maxsize=1)
def channels() -> pl.DataFrame:
    """Справочник каналов с названиями. Кэшируется: файл невелик и не меняется
    в пределах запуска."""
    return pl.read_parquet(PATHS.interim / "channels.parquet")


@functools.lru_cache(maxsize=1)
def _by_channel() -> dict:
    cols = ["ch", "obj", "obj_name", "obj_parent", "obj_parent_name",
            "obj_kind", "sname", "tag", "stype", "picket"]
    have = [c for c in cols if c in channels().columns]
    return {r["ch"]: r for r in channels().select(have).to_dicts()}


@functools.lru_cache(maxsize=1)
def _by_object() -> dict:
    df = channels()
    cols = [c for c in ("obj", "obj_name", "obj_parent", "obj_parent_name",
                        "obj_kind") if c in df.columns]
    out = {}
    for r in df.select(cols).unique(subset=["obj"]).to_dicts():
        out[r["obj"]] = r
    return out


@functools.lru_cache(maxsize=1)
def _segment_has_picket() -> set:
    """Пары (объект, участок), где пикет действительно известен.

    Нужно, чтобы отличить участок ПК 0-10 от строк, у которых пикета нет и
    участок посчитался нулём по умолчанию.
    """
    df = channels()
    if "picket" not in df.columns:
        return set()
    known = df.filter(pl.col("picket").is_not_null()).with_columns(
        (pl.col("picket") // SEG_SIZE).cast(pl.Int64).alias("seg"))
    return {(r["obj"], r["seg"]) for r in known.select(["obj", "seg"]).to_dicts()}


def reset_cache() -> None:
    for f in (channels, _by_channel, _by_object, _segment_has_picket):
        f.cache_clear()


def segment_label(obj: str | None, seg: int | None) -> str:
    """Подпись участка для линейной схемы."""
    if seg is None:
        return AMBIGUOUS_SEGMENT
    if seg == 0 and (obj, 0) not in _segment_has_picket():
        # Участок посчитался нулём из-за отсутствия пикета, а не потому, что
        # объект начинается в нуле.
        return AMBIGUOUS_SEGMENT
    lo = int(seg * SEG_SIZE)
    return f"ПК {lo}\u2013{lo + int(SEG_SIZE)}"


def for_channel(ch: int | None) -> dict:
    """Адресные поля канала. Канал вне справочника — 1 142 из 12 627 —
    помечается явно, а не отдаётся пустыми строками."""
    if ch is None:
        return {}
    row = _by_channel().get(int(ch))
    if row is None:
        return {"obj_name": UNKNOWN, "sensor_name": UNKNOWN, "address_known": False}
    return {
        "obj": row.get("obj"),
        "obj_name": row.get("obj_name"),
        "obj_parent": row.get("obj_parent"),
        "obj_parent_name": row.get("obj_parent_name"),
        "obj_kind": row.get("obj_kind"),
        "obj_kind_ru": OBJ_KIND_RU.get(row.get("obj_kind") or ""),
        "sensor_name": row.get("sname"),
        "tag": row.get("tag"),
        "sensor_type": row.get("stype"),
        "picket": row.get("picket"),
        "picket_label": (f"ПК {row['picket']:g}" if row.get("picket") is not None
                         else UNKNOWN_PICKET),
        "address_known": True,
    }


def for_object(obj: str | None) -> dict:
    if obj is None:
        return {}
    row = _by_object().get(str(obj))
    if row is None:
        return {"obj": obj, "obj_name": UNKNOWN, "address_known": False}
    return {
        "obj": row.get("obj"),
        "obj_name": row.get("obj_name"),
        "obj_parent": row.get("obj_parent"),
        "obj_parent_name": row.get("obj_parent_name"),
        "obj_kind": row.get("obj_kind"),
        "obj_kind_ru": OBJ_KIND_RU.get(row.get("obj_kind") or ""),
        "address_known": True,
    }


def describe(obj: str | None = None, ch: int | None = None,
             seg: int | None = None) -> dict:
    """Полный адрес: по каналу, если он есть, иначе по объекту."""
    out = for_channel(ch) if ch is not None else for_object(obj)
    if seg is not None:
        out["segment"] = seg
        out["segment_label"] = segment_label(out.get("obj") or obj, seg)
    return out
