"""Почему сработал ЭТОТ алерт.

Важность признаков по gain отвечает на другой вопрос — какие признаки модель
использует вообще. Для строки она бесполезна: признак с высокой общей важностью
может в конкретном алерте не сыграть никакой роли, а решающим окажется тот, что
в среднем не важен.

TreeSHAP даёт вклад каждого признака в предсказание конкретной строки, причём
вклады складываются ровно в отклонение от базового значения — то есть их можно
показывать диспетчеру, не обманывая. И XGBoost, и LightGBM считают его
встроенно, отдельная библиотека не нужна.

Показывать сырые имена признаков диспетчеру бессмысленно, поэтому рядом лежит
словарь человеческих названий. Признак без перевода отдаётся как есть: лучше
непонятное имя, чем выдуманное объяснение.
"""
import numpy as np

# Человеческие названия. Пополняется по мере того, как признаки попадают в
# верхушку объяснений — переводить все 225 заранее смысла нет.
FEATURE_LABELS = {
    # Факторы A_link, которые чаще всего попадают в объяснение (замер 28.09 по выданным
    # алертам 02–29.06: без этих подписей 37 % факторов шли сырым именем). Формулировки —
    # по определению признака: src/mkl/panel.py, features/base.py, features/lifecycle.py.
    "n_active_hours": "часов с событиями за сутки",
    "activity_days_ratio": "активные дни за неделю к активным за месяц",
    "prev_gap_days_max_w30": "самый долгий перерыв в данных за месяц",
    "workhours_frac": "доля событий с 9 до 18 часов",
    "max_gap_s": "самый долгий промежуток без событий за сутки",
    "night_frac": "доля событий с 0 до 6 часов",
    "dow_active_rate": "как часто канал работает в этот день недели",
    "events_accel": "события за неделю к среднему за месяц",
    "n_fire": "срабатывания пожарных датчиков за сутки",
    "val_ok_med": "медиана исправных показаний за сутки",
    "val_ok_min": "минимум исправных показаний за сутки",
    "n_on": "состояния «Включен» за сутки",
    "days_since_same_dow": "дней с прошлого такого же дня недели с данными",
    "n_flood_bins_sum_w7": "окна с лавиной тревог за неделю",
    "n_events_std_w7": "разброс числа событий за неделю",
    "n_intrusion": "срабатывания датчиков проникновения",
    "n_alarms": "число тревог за сутки",
    "n_alarms_w7": "тревоги за неделю",
    "n_alarms_w30": "тревоги за месяц",
    "n_bad": "события неисправности за сутки",
    "n_bad_w7": "неисправности за неделю",
    "n_bad_w30": "неисправности за месяц",
    "time_in_alarm_s": "время в тревоге за сутки",
    "time_in_bad_s": "время в неисправном состоянии",
    "n_standing_4h": "тревоги дольше четырёх часов",
    "n_stale_24h": "тревоги дольше суток",
    "is_guard_object": "объект охранной зоны",
    "obj_armed": "объект на охране",
    "days_since_arm_event": "суток с последней смены режима охраны",
    "n_battery_power": "переходы на питание от батарей",
    "n_many_bad": "сообщения «много неисправных устройств»",
    "chatter_psi": "дребезг канала",
    "chatter_psi_alarm": "дребезг тревог",
    "n_chatter_1min": "срабатывания чаще раза в минуту",
    "n_chatter_1min_w7": "дребезг за неделю",
    "n_flood_bins": "окна с лавиной тревог",
    "max_alarm_10min": "пик тревог за десять минут",
    "silence_z": "необычно долгое молчание",
    "silence_ratio": "доля молчания",
    "days_since_last_bad": "суток с последней неисправности",
    "days_since_last_alarm": "суток с последней тревоги",
    "days_since_prior_episode": "суток с прошлого эпизода",
    "age_days": "возраст канала в журнале",
    "n_active_days_w7": "активных суток за неделю",
    "n_active_days_w30": "активных суток за месяц",
    "prev_gap_days": "разрыв до предыдущего отчёта",
    "prev_gap_days_mean_w30": "обычный ритм опроса",
    "gap_vs_own_rhythm": "разрыв против обычного ритма",
    "obj_n_bad": "неисправности на объекте",
    "obj_frac_bad": "доля больных каналов объекта",
    "share_obj_alarms": "доля тревог объекта на этом канале",
    "par_n_bad": "неисправности на соседях по комплексу",
    "par_frac_objects_bad": "доля больных объектов комплекса",
    "hhi_alarm": "концентрация тревог на одном канале",
    "top1_share_alarm": "доля тревог худшего канала",
    "n_pump_on": "включения насосов",
    "pump_switches": "переключения насосов",
    "n_flood": "срабатывания датчиков затопления",
    "t_mean": "средняя температура за сутки",
    "precip_mm": "осадки за сутки",
    "api_90": "накопленное увлажнение грунта",
    "dow": "день недели",
    "is_holiday": "праздничный день",
    "doy_sin": "сезон",
    "doy_cos": "сезон",
}


def label(feature: str) -> str:
    return FEATURE_LABELS.get(feature, feature)


def contributions(model, X: np.ndarray, feature_names: list[str],
                  top: int = 5) -> list[list[dict]]:
    """Вклады признаков в каждую строку. Возвращает по `top` на строку.

    Отбираются вклады, толкающие риск ВВЕРХ: диспетчеру нужно знать, почему
    алерт есть, а не почему он мог бы быть меньше.
    """
    if len(X) == 0:
        return []
    raw = _shap(model, X)
    if raw is None:
        return [[] for _ in range(len(X))]
    out = []
    for row in raw:
        vals = row[:len(feature_names)]
        order = np.argsort(-vals)[:top]
        out.append([
            {"feature": feature_names[i],
             "label": label(feature_names[i]),
             "contribution": round(float(vals[i]), 4)}
            for i in order if vals[i] > 0
        ])
    return out


def _shap(model, X: np.ndarray):
    """Вклады по TreeSHAP средствами самого бэкенда."""
    try:
        if hasattr(model, "get_booster"):           # XGBoost
            import xgboost as xgb
            return model.get_booster().predict(
                xgb.DMatrix(X, feature_names=None), pred_contribs=True)
        if hasattr(model, "booster_"):              # LightGBM
            return model.booster_.predict(X, pred_contrib=True)
        if hasattr(model, "get_feature_importance"):  # CatBoost
            from catboost import Pool
            return model.get_feature_importance(Pool(X), type="ShapValues")
    except Exception as exc:                        # noqa: BLE001
        print(f"вклады признаков недоступны ({type(exc).__name__}: {exc})",
              flush=True)
    return None
