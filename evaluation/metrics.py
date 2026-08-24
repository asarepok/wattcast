"""
Shared evaluation metrics, computed identically for ARIMA, Random Forest,
and LSTM so the model comparison is a fair, apples-to-apples comparison.
"""

import numpy as np


def mae(y_true, y_pred):
    return float(np.mean(np.abs(y_true - y_pred)))


def rmse(y_true, y_pred):
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def mape(y_true, y_pred, threshold=1.0):
    """
    Mean Absolute Percentage Error, computed only over hours where the
    panel was actually producing meaningful power (actual > threshold
    watts). Solar power is exactly 0 for roughly half of every day
    (nighttime); since MAPE divides by the actual value, those hours
    would make it numerically unstable. Daytime-only MAPE is standard
    practice in solar forecasting for this reason.
    """
    mask = y_true > threshold
    if not np.any(mask):
        return float("nan")
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100)


def r2(y_true, y_pred):
    ss_res = np.sum((y_true - y_pred) ** 2)
    ss_tot = np.sum((y_true - np.mean(y_true)) ** 2)
    return float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")


def evaluate(y_true, y_pred):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return {
        "MAE": mae(y_true, y_pred),
        "RMSE": rmse(y_true, y_pred),
        "MAPE": mape(y_true, y_pred),
        "R2": r2(y_true, y_pred),
    }
