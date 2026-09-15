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
    return {
        "op_feasible": bool(op.get("feasible", False)),
        "op_precision": float(op.get("precision", op.get("max_precision", float("nan")))),
        "op_recall": float(op.get("recall", op.get("recall_at_max_precision", float("nan")))),
        "op_k": int(op.get("k", op.get("k_at_max_precision", 0))),
        "n": int(len(y)),
        "n_pos": int(y.sum()),
        "base_rate": base_rate(y),
        "pr_auc": pr_auc(y, p),
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
