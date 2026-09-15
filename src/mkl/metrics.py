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


def summary(y: np.ndarray, p: np.ndarray, budget: int) -> dict:
    y = np.asarray(y)
    p = np.asarray(p, dtype=float)
    thr = threshold_for_budget(p, budget)
    pred = p >= thr
    tp = int((pred & (y == 1)).sum())
    return {
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
