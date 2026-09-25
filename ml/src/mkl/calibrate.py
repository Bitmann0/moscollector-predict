import numpy as np
from sklearn.isotonic import IsotonicRegression


def fit_isotonic(p: np.ndarray, y: np.ndarray) -> IsotonicRegression:
    """Изотоническая калибровка.

    Применяется вместо oversampling: сдвиг порога и калибровка дают тот же
    эффект на редком классе, не портя вероятностные оценки.
    """
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(np.asarray(p, dtype=float), np.asarray(y, dtype=float))
    return iso


def apply(iso: IsotonicRegression, p: np.ndarray) -> np.ndarray:
    return np.clip(iso.predict(np.asarray(p, dtype=float)), 0.0, 1.0)
