import numpy as np
from sklearn.metrics import average_precision_score, brier_score_loss


def pr_auc(y: np.ndarray, p: np.ndarray) -> float:
    if y.sum() == 0 or y.sum() == len(y):
        return float("nan")
    return float(average_precision_score(y, p))


def base_rate(y: np.ndarray) -> float:
    return float(y.sum() / len(y)) if len(y) else float("nan")


def lift(y: np.ndarray, p: np.ndarray, k: int) -> float:
    """Во сколько раз точность на топ-k выше базовой ставки."""
    br = base_rate(y)
    return precision_at_k(y, p, k) / br if br > 0 else float("nan")


def pr_auc_norm(y: np.ndarray, p: np.ndarray) -> float:
    """Доля отрыва от случайного ранжирования, которую забрала модель.

    PR-AUC случайного ранжировщика равен базовой ставке, поэтому сырой PR-AUC
    нельзя сравнивать между постановками с разной редкостью события: 0.96 при
    базовой ставке 0.62 хуже, чем 0.28 при базовой ставке 0.03. Нормировка даёт
    0 для случайного и 1 для идеального при любой базовой ставке и не
    вознаграждает раздувание доли позитивов.
    """
    br = base_rate(y)
    a = pr_auc(y, p)
    if a != a or br >= 1.0:
        return float("nan")
    return float((a - br) / (1.0 - br))


def _top_k_mask(p: np.ndarray, k: int) -> np.ndarray:
    k = min(int(k), len(p))
    mask = np.zeros(len(p), dtype=bool)
    if k > 0:
        mask[np.argsort(-p, kind="stable")[:k]] = True
    return mask


def precision_at_k(y: np.ndarray, p: np.ndarray, k: int) -> float:
    m = _top_k_mask(p, k)
    return float(y[m].sum() / m.sum()) if m.sum() else float("nan")


def recall_at_k(y: np.ndarray, p: np.ndarray, k: int) -> float:
    m = _top_k_mask(p, k)
    return float(y[m].sum() / y.sum()) if y.sum() else float("nan")


def threshold_for_budget(p: np.ndarray, budget: int) -> float:
    budget = min(int(budget), len(p))
    if budget <= 0:
        return float("inf")
    return float(np.sort(p)[::-1][budget - 1])


def precision_at_recall(y: np.ndarray, p: np.ndarray,
                        target_recall: float = 0.5) -> dict:
    """Точность в точке, где Recall впервые достигает заданного уровня.

    Прямой ответ на вопрос ТЗ «Recall > 0.5»: если ловить половину событий,
    какой ценой по ложным срабатываниям. В отличие от максимума точности по
    всем порогам, эта величина не вырождается в 1.0 на одном верхнем алерте.
    """
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    n_pos = int(y.sum())
    if n_pos == 0:
        return {"precision": float("nan"), "k": 0, "alerts_per_positive": float("nan")}
    order = np.argsort(-p, kind="stable")
    tp = np.cumsum(y[order])
    k = np.arange(1, len(y) + 1)
    rec = tp / n_pos
    idx = np.searchsorted(rec, target_recall, side="left")
    if idx >= len(rec):
        idx = len(rec) - 1
    return {
        "precision": float(tp[idx] / k[idx]),
        "k": int(k[idx]),
        "recall": float(rec[idx]),
        "alerts_per_positive": float(k[idx] / max(tp[idx], 1)),
    }


def target_operating_point(y: np.ndarray, p: np.ndarray,
                           min_precision: float = 0.7) -> dict:
    """Точка с максимальным Recall среди тех, где Precision >= min_precision.

    Именно так проверяется требование ТЗ «Precision > 0.7 и Recall > 0.5»:
    вопрос не в том, какова точность при произвольно выбранном бюджете, а в том,
    существует ли вообще порог, удовлетворяющий обоим условиям одновременно.
    Фиксированный бюджет для этого не годится: если алертов больше, чем
    позитивов, точность ограничена сверху их отношением независимо от модели.
    """
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    n_pos = int(y.sum())
    if n_pos == 0 or len(y) == 0:
        return {"feasible": False, "reason": "нет позитивов"}

    order = np.argsort(-p, kind="stable")
    ys = y[order]
    tp = np.cumsum(ys)
    k = np.arange(1, len(ys) + 1)
    prec = tp / k
    rec = tp / n_pos

    ok = prec >= min_precision
    if not ok.any():
        best = int(np.argmax(prec))
        return {
            "feasible": False,
            "reason": f"Precision нигде не достигает {min_precision}",
            "max_precision": float(prec.max()),
            "recall_at_max_precision": float(rec[best]),
            "k_at_max_precision": int(k[best]),
        }
    i = int(np.argmax(np.where(ok, rec, -1.0)))
    return {
        "feasible": True,
        "precision": float(prec[i]),
        "recall": float(rec[i]),
        "k": int(k[i]),
        "threshold": float(p[order][i]),
        "alerts_per_1000_entities": float(1000.0 * k[i] / len(y)),
    }


def budget_curve(y: np.ndarray, p: np.ndarray,
                 budgets: tuple[int, ...]) -> list[dict]:
    """Precision и Recall при нескольких бюджетах алертов.

    Одна точка k скрывает форму кривой: модель может быть точной на первых
    десятках алертов и бесполезной на сотнях. Диспетчеру важна именно вершина.
    """
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    return [{
        "budget": int(b),
        "precision": precision_at_k(y, p, b),
        "recall": recall_at_k(y, p, b),
        "lift": lift(y, p, b),
    } for b in budgets if b <= len(p)]


def summary(y: np.ndarray, p: np.ndarray, budget: int) -> dict:
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    thr = threshold_for_budget(p, budget)
    pred = p >= thr
    tp = int((pred & (y == 1)).sum())
    op = target_operating_point(y, p, min_precision=0.7)
    par = precision_at_recall(y, p, target_recall=0.5)
    return {
        "op_feasible": bool(op.get("feasible", False)),
        "op_precision": float(op.get("precision", op.get("max_precision", float("nan")))),
        "op_recall": float(op.get("recall", op.get("recall_at_max_precision", float("nan")))),
        "op_k": int(op.get("k", op.get("k_at_max_precision", 0))),
        "p_at_r50": par["precision"],
        "k_at_r50": par["k"],
        "n": int(len(y)),
        "n_pos": int(y.sum()),
        "base_rate": base_rate(y),
        "pr_auc": pr_auc(y, p),
        "pr_auc_norm": pr_auc_norm(y, p),
        "precision_at_k": precision_at_k(y, p, budget),
        "recall_at_k": recall_at_k(y, p, budget),
        "lift_at_k": lift(y, p, budget),
        "precision": float(tp / pred.sum()) if pred.sum() else float("nan"),
        "recall": float(tp / y.sum()) if y.sum() else float("nan"),
        "threshold": thr,
        "budget": int(budget),
        "brier": (float(brier_score_loss(y, np.clip(p, 0, 1)))
                  if len(np.unique(y)) > 1 else float("nan")),
    }


def episode_summary(entity: np.ndarray, day: np.ndarray, y: np.ndarray,
                    p: np.ndarray, horizon_days: int = 1,
                    budget: int | None = None) -> dict:
    """Метрика на уровне эпизодов, а не канало-суток.

    Подряд идущие положительные сутки одной сущности — один эпизод: канал,
    лежащий месяц, должен считаться одним событием, которое надо было
    предсказать один раз, а не тридцатью независимыми попаданиями. Посуточный
    счёт завышает качество тем сильнее, чем длиннее отказы: на отложенном
    периоде 5% эпизодов длиной 8-30 суток давали 28% положительных суток.

    Эпизод считается пойманным, если хотя бы в одни сутки внутри окна
    горизонта перед его началом скор превысил порог. Точность считается по
    сущностям-суткам, как обычно, потому что ложная тревога стоит работы
    диспетчера независимо от того, в какой эпизод она попала.
    """
    entity = np.asarray(entity)
    day = np.asarray(day, dtype="datetime64[D]")
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)

    order = np.lexsort((day, entity))
    e, d, yy, pp = entity[order], day[order], y[order], p[order]

    # начало эпизода: позитив, перед которым у той же сущности не было позитива
    # в предыдущие сутки
    prev_e = np.roll(e, 1)
    prev_d = np.roll(d, 1)
    prev_y = np.roll(yy, 1)
    same = (e == prev_e) & ((d - prev_d) == np.timedelta64(1, "D"))
    # Первая строка после сортировки не имеет предшественника, поэтому
    # продолжением эпизода быть не может.
    if len(same):
        same[0] = False
    starts = (yy == 1) & ~(same & (prev_y == 1))

    n_ep = int(starts.sum())
    if n_ep == 0:
        return {"episodes": 0, "episode_recall": float("nan"),
                "episode_precision": float("nan")}

    def recall_at(threshold: float) -> float:
        hit = pp >= threshold
        caught = 0
        idx = np.flatnonzero(starts)
        for i in idx:
            lo = d[i] - np.timedelta64(horizon_days, "D")
            j = i
            ok = False
            while j >= 0 and e[j] == e[i] and d[j] >= lo:
                if hit[j]:
                    ok = True
                    break
                j -= 1
            caught += ok
        return caught / n_ep

    # Бюджет задаётся снаружи — столько алертов сервис реально выдаст.
    # Прежде он брался как 0.7 от числа эпизодов В ТЕСТЕ, то есть из меток:
    # метрика зависела от того, сколько отказов случилось, и между фолдами
    # разного размера была несопоставима.
    k = int(budget) if budget else int(np.ceil(0.7 * n_ep))
    thr = threshold_for_budget(p, k)
    pred = p >= thr
    tp_days = int((pred & (y == 1)).sum())
    return {
        "episodes": n_ep,
        "episode_recall": recall_at(thr),
        "episode_precision": float(tp_days / pred.sum()) if pred.sum() else float("nan"),
        "episode_threshold": float(thr),
        "episode_budget": int(k),
        "days_per_episode": float(y.sum() / n_ep),
    }


def at_threshold(y: np.ndarray, p: np.ndarray, thr: float) -> dict:
    """Точность и полнота в ЗАРАНЕЕ заданной точке.

    Отличается от target_operating_point тем, что порог сюда приходит снаружи,
    а не подбирается по этим же ответам. Подбор порога на том же множестве, где
    потом рапортуется точность, — это оракул: в отчёте по отложенному периоду
    op_precision выходил равным 0.7000245 и 0.7003284, то есть прижатым к
    ограничению до четвёртого знака, чего при заранее выбранном пороге не бывает.

    Здесь же считается, сколько алертов эта точка требует в сутки: заявлять
    взятую цель в режиме, которого сервис не выдаст, нельзя.
    """
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    sel = p >= thr
    k = int(sel.sum())
    n_pos = int(y.sum())
    tp = int(y[sel].sum()) if k else 0
    return {
        "threshold": float(thr),
        "k": k,
        "precision": float(tp / k) if k else float("nan"),
        "recall": float(tp / n_pos) if n_pos else float("nan"),
        "tp": tp,
    }


def meets_target(op: dict, n_days: int, budget_per_day: int,
                 min_precision: float = 0.7, min_recall: float = 0.5) -> dict:
    """Взята ли цель ТЗ — с проверкой, что точка достижима на бюджете.

    Цель, достигнутая при 427 алертах в сутки, когда в конфигурации головы
    стоит 20, — это не взятая цель, а другой режим работы. Прежде вердикт
    выносился без этой проверки, и голова A отчитывалась о полноте 0.633 в
    точке, которую serve.py никогда не выдаст: на своём бюджете та же модель
    давала полноту 0.038.
    """
    budget = budget_per_day * n_days
    k = op.get("k", 0) or 0
    within = k <= budget
    ok = (op.get("precision", 0) >= min_precision
          and op.get("recall", 0) >= min_recall and within)
    return {
        "meets_target": bool(ok),
        "within_budget": bool(within),
        "alerts_per_day": float(k / n_days) if n_days else float("nan"),
        "budget_per_day": budget_per_day,
        "budget_overrun": float(k / budget) if budget else float("nan"),
    }
