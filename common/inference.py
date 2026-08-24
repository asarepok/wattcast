"""
Unified inference interface for the trained forecasting models. This is
the one place backend code calls to get a power forecast, regardless of
which underlying model serves it. import predict() directly (no
separate service, in-process function call).

    from common.inference import predict
    predict(part_id, "2026-08-20", model="random_forest", quantity=12)
    predict(part_id, "2026-08-20", "2026-08-22", model="random_forest", quantity=12)

Returns a pandas Series of hourly predicted-watt values (24 per day in
the requested range; a single date with no end_date is one day),
already scaled by how many physical units the user owns.
Quantity is applied as a simple post-prediction multiplier rather than
a model input. see project notes on why (identical modules wired in
parallel add their currents/power roughly linearly, and none of these
models were ever trained on varying array sizes to learn that
relationship themselves).
"""

import json
import os
import sqlite3
from collections import namedtuple

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from common.dataset import EXCLUDED_PART_IDS
from common.features import FEATURES, PANEL_FEATURES, WEATHER_FEATURES
from common.weather_api import get_forecast

MODELS_DIR = os.path.join(os.path.dirname(__file__), "..", "models")
PANEL_DB = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "panel_cache.db")

# .power: the 24 hourly predicted-watt values (what predict() alone returns).
# .weather: DataFrame indexed by time with shortwave_radiation_wm2 and
#   temperature_2m_c for the target day, or None for ARIMA, which never
#   sees weather at all.
ForecastResult = namedtuple("ForecastResult", ["power", "weather"])


def get_panel_specs(part_id):
    """
    Looks up a panel's rated specs from panel_cache.db. Used both to
    build the panel-spec feature columns at inference time, and by
    forecast_cli.py to print a baseline for comparison after a forecast.
    """
    conn = sqlite3.connect(PANEL_DB)
    row = conn.execute(
        "SELECT manufacturer, model_name, pmp, voc, isc, vmp, imp, n_series, n_parallel "
        "FROM panels WHERE part_id = ?",
        (part_id,),
    ).fetchone()
    conn.close()
    if row is None:
        raise ValueError(f"Unknown part_id: '{part_id}'")
    keys = ["manufacturer", "model_name", "pmp", "voc", "isc", "vmp", "imp", "n_series", "n_parallel"]
    return dict(zip(keys, row))

# ARIMA is fit per-panel and cannot generalize to a panel it wasn't
# specifically trained on (no panel-spec input). This is the fixed
# list of part_ids it can ever serve. Random Forest and LSTM don't have
# that restriction once trained (panel specs are literal inputs, so they
# generalize across panels), but they DO share a separate restriction:
# EXCLUDED_PART_IDS (the 2 AzurSpace research cells) were dropped from
# their training data entirely, so neither model has ever seen them.
ARIMA_PANELS = {
    "FirstSolar|FS_390_PLUS|simscape.electrical.solarCell": "FS_390_PLUS",
    "Amerisolar|AS_6M30_HC_320W|simscape.electrical.solarCell": "AS_6M30_HC_320W",
    "Telesun|TP7F78M_H_590|simscape.electrical.solarCell": "TP7F78M_H_590",
}

_model_cache = {}


def _check_not_excluded(part_id, model_name):
    if part_id in EXCLUDED_PART_IDS:
        raise ValueError(
            f"'{part_id}' was excluded from {model_name}'s training data "
            f"(AzurSpace bare research cells; their simulated output is "
            f"a load/circuit-mismatch artifact, not real physics; see "
            f"common/dataset.py). Not supported for forecasting."
        )


def _load_arima(part_id):

    if part_id not in ARIMA_PANELS:
        raise ValueError(
            f"ARIMA was only trained on 3 representative panels, not "
            f"'{part_id}'. Available: {list(ARIMA_PANELS)}. Use "
            f"model='random_forest' or model='lstm' for any other panel."
        )

    label = ARIMA_PANELS[part_id]
    cache_key = ("arima", label)

    if cache_key not in _model_cache:
        model_path = os.path.join(MODELS_DIR, "arima", f"{label}.pkl")
        meta_path = os.path.join(MODELS_DIR, "arima", f"{label}_meta.json")
        model = joblib.load(model_path)
        with open(meta_path) as f:
            meta = json.load(f)
        _model_cache[cache_key] = (model, meta)

    return _model_cache[cache_key]


def _num_days(start_date, end_date):
    """Inclusive day count spanning start_date..end_date (1 if end_date is None)."""
    target_start = pd.Timestamp(start_date).normalize()
    target_end = pd.Timestamp(end_date).normalize() if end_date is not None else target_start
    return int((target_end - target_start) / pd.Timedelta(days=1)) + 1


def _predict_arima(part_id, start_date, end_date, quantity, latitude=None, longitude=None,
                    on_weather_fetched=None):
    # latitude/longitude accepted only so _DISPATCH can call every model
    # the same way. ARIMA never sees weather, so location is a no-op.
    # on_weather_fetched is also accepted but never called: ARIMA has no
    # weather-fetch step to hook into, so a caller wanting a simulation
    # comparison against it has to fetch weather and trigger that
    # itself (see backend/app.py).
    # ARIMA never sees weather: it can only extrapolate forward from
    # wherever its training data ends (recorded in train_end at fit
    # time, see training/train_arima.py). The further start_date is
    # past that point, the less reliable the extrapolation gets; this
    # is expected of the weather-blind baseline, not a bug.

    model, meta = _load_arima(part_id)

    train_end = pd.Timestamp(meta["train_end"])
    target_start = pd.Timestamp(start_date).normalize()
    num_hours = _num_days(start_date, end_date) * 24
    hours_ahead = int((target_start - train_end) / pd.Timedelta(hours=1))

    if hours_ahead <= 0:
        raise ValueError(
            f"start_date must be after this model's training data ends "
            f"({train_end}). ARIMA can only extrapolate forward from there."
        )

    forecast = model.predict(n_periods=hours_ahead + num_hours)
    # .predict() returns a Series whose index continues counting from
    # the training data's row numbers (e.g. 95712, 95713, ...), not
    # from 0. Pulling .to_numpy() strips that index before building the
    # new one below, otherwise pd.Series(day_values, index=...) tries
    # to ALIGN by those old integer labels against the new date labels,
    # finds no overlap, and silently fills every value with NaN.
    day_values = forecast.iloc[hours_ahead:hours_ahead + num_hours].to_numpy()

    index = pd.date_range(start=target_start, periods=num_hours, freq="h")
    power = pd.Series(day_values, index=index) * quantity
    return ForecastResult(power=power, weather=None)


def _load_random_forest():
    cache_key = "random_forest"
    if cache_key not in _model_cache:
        model_path = os.path.join(MODELS_DIR, "random_forest", "random_forest.pkl")
        model = joblib.load(model_path)
        # verbose=3 was set for training (see training/train_random_forest.py)
        # and is saved as part of the pickled estimator, so it fires on
        # every .predict() call too unless turned off here, since training
        # verbosity was requested, not inference-time joblib logging.
        model.set_params(verbose=0)
        _model_cache[cache_key] = model
    return _model_cache[cache_key]


def _predict_random_forest(part_id, start_date, end_date, quantity, latitude=None, longitude=None,
                            on_weather_fetched=None):
    _check_not_excluded(part_id, "Random Forest")

    specs = get_panel_specs(part_id)
    model = _load_random_forest()

    # Live weather for the requested range (get_forecast already
    # treats end_date=None as a single day), same column names
    # training used (see common/weather_api.py). This is the whole
    # reason RF can react to a specific date, unlike ARIMA.
    # latitude/longitude default to the training site inside
    # get_forecast() when omitted. RF has no temporal windowing, so
    # spanning more days is just more independent feature rows.
    df = get_forecast(start_date, end_date, latitude=latitude, longitude=longitude)

    # Fires the instant weather is in hand, before the (fast) feature
    # building and model.predict() below even run, so a caller wanting
    # to also run a slow simulation against this same weather (see
    # backend/app.py) can kick that off now and have it run genuinely
    # concurrently with the rest of this prediction, not after it.
    if on_weather_fetched is not None:
        on_weather_fetched(df.copy())

    # Panel-spec + time features, built exactly the way common/features.py
    # /training/train_random_forest.py built them (same column names,
    # same panel_cache.db field -> rated_* mapping).
    df["rated_pmax_w"] = specs["pmp"]
    df["rated_voc_v"] = specs["voc"]
    df["rated_isc_a"] = specs["isc"]
    df["rated_vmp_v"] = specs["vmp"]
    df["rated_imp_a"] = specs["imp"]
    df["n_series"] = specs["n_series"]
    df["n_parallel"] = specs["n_parallel"]
    df["month"] = df["time"].dt.month
    df["hour"] = df["time"].dt.hour

    predictions = model.predict(df[FEATURES])
    # No clipping here. This is the model's raw, unadjusted output,
    # on purpose (per user request, to see real model behavior rather
    # than a post-hoc-corrected version).

    power = pd.Series(predictions, index=df["time"]) * quantity
    weather = df.set_index("time")[["shortwave_radiation_wm2", "temperature_2m_c"]]
    return ForecastResult(power=power, weather=weather)


class LSTMModel(nn.Module):
    # Must match training/train_lstm.py's architecture exactly: only
    # the learned weights are saved (lstm.pth is a state_dict, not the
    # whole object like joblib gives ARIMA/RF), so the class shape has
    # to be reconstructed identically before those weights can be loaded.
    def __init__(self, n_weather, n_static, hidden_size=32):
        super().__init__()
        self.lstm = nn.LSTM(input_size=n_weather, hidden_size=hidden_size, batch_first=True)
        self.fc1 = nn.Linear(hidden_size + n_static, 16)
        self.relu = nn.ReLU()
        self.fc2 = nn.Linear(16, 1)

    def forward(self, weather_seq, static):
        out, _ = self.lstm(weather_seq)
        combined = torch.cat([out[:, -1, :], static], dim=1)
        return self.fc2(self.relu(self.fc1(combined)))


def _load_lstm():
    cache_key = "lstm"
    if cache_key not in _model_cache:
        model = LSTMModel(len(WEATHER_FEATURES), len(PANEL_FEATURES))
        state_dict = torch.load(
            os.path.join(MODELS_DIR, "lstm", "lstm.pth"),
            map_location="cpu",  # inference is cheap enough that CPU is fine even though training used GPU
        )
        model.load_state_dict(state_dict)
        model.eval()

        scalers = joblib.load(os.path.join(MODELS_DIR, "lstm", "scalers.pkl"))

        _model_cache[cache_key] = (model, scalers)

    return _model_cache[cache_key]


def _predict_lstm(part_id, start_date, end_date, quantity, latitude=None, longitude=None,
                   on_weather_fetched=None):
    _check_not_excluded(part_id, "LSTM")

    specs = get_panel_specs(part_id)
    model, scalers = _load_lstm()

    target_start = pd.Timestamp(start_date).normalize()
    num_hours = _num_days(start_date, end_date) * 24

    # Predicting the range's very first hour (00:00) needs a 24-hour
    # weather window reaching back into the previous day, so fetch the
    # day before through the last requested day. This gives exactly
    # enough data to build every overlapping window needed, however
    # many days are in the range.
    window_start = target_start - pd.Timedelta(days=1)
    window_end = target_start + pd.Timedelta(hours=num_hours - 1)
    weather = get_forecast(window_start, window_end, latitude=latitude, longitude=longitude)

    # Fires as soon as weather is in hand, same as _predict_random_forest,
    # so a caller can run a simulation concurrently with the rest of this
    # prediction. Trimmed to just the target range first (dropping the
    # lookback day added above), since that's the same range a
    # simulation comparison needs to match, not LSTM's own extra window.
    if on_weather_fetched is not None:
        on_weather_fetched(weather[weather["time"] >= target_start].copy())

    weather_scaled = scalers["weather"].transform(weather[WEATHER_FEATURES]).astype("float32")

    # windows[h] = the 24 hours ending at hour h of the target range.
    windows = np.stack([weather_scaled[h:h + 24] for h in range(num_hours)])  # [num_hours, 24, 7]

    # A named DataFrame (not a bare array) so the fitted scaler (fit
    # on train_df[PANEL_FEATURES] in training) sees matching column
    # names instead of just matching positions.
    static_raw = pd.DataFrame([{
        "rated_pmax_w": specs["pmp"],
        "rated_voc_v": specs["voc"],
        "rated_isc_a": specs["isc"],
        "rated_vmp_v": specs["vmp"],
        "rated_imp_a": specs["imp"],
        "n_series": specs["n_series"],
        "n_parallel": specs["n_parallel"],
    }])[PANEL_FEATURES]
    static_scaled = scalers["panel"].transform(static_raw).astype("float32")
    static_batch = np.repeat(static_scaled, num_hours, axis=0)  # [num_hours, 7]

    with torch.no_grad():
        pred_scaled = model(torch.from_numpy(windows), torch.from_numpy(static_batch)).numpy()

    pred_w = scalers["target"].inverse_transform(pred_scaled).clip(min=0).ravel()

    index = pd.date_range(start=target_start, periods=num_hours, freq="h")
    power = pd.Series(pred_w, index=index) * quantity

    # weather covers one extra day before the range. Keep just the
    # requested range for display, aligned with `power` above.
    target_weather = (
        weather[weather["time"] >= target_start]
        .set_index("time")[["shortwave_radiation_wm2", "temperature_2m_c"]]
    )
    return ForecastResult(power=power, weather=target_weather)


_DISPATCH = {
    "arima": _predict_arima,
    "random_forest": _predict_random_forest,
    "lstm": _predict_lstm,
}


def predict_with_weather(part_id, start_date, end_date=None, model="random_forest", quantity=1,
                          latitude=None, longitude=None, on_weather_fetched=None):
    """
    Forecast hourly power output for a panel over start_date..end_date
    (inclusive), plus the forecasted irradiance/temperature behind it.

    part_id: the panel's Simscape part_id, e.g.
                "Amerisolar|AS_6M30_HC_320W|simscape.electrical.solarCell"
    start_date: the first date to forecast (any pandas-parseable date/string)
    end_date: the last date to forecast, inclusive; defaults to
                start_date (a single day, 24 hours) when omitted.
    model: "arima" | "random_forest" | "lstm"
    quantity: how many physical units of this panel the user owns
    latitude, longitude: site to fetch live weather for (random_forest/
                lstm only; ARIMA never sees weather). Defaults to the
                training site when either is omitted, e.g. a caller with
                no geolocation available. Note the models were trained on
                weather ranges from one site's climate; forecasts for a
                site with very different conditions (e.g. much higher
                latitude, different humidity/cloud regime) are an
                extrapolation outside that training range.
    on_weather_fetched: optional callback, called with the weather
                DataFrame (matching the target range, "time" as a
                column) the instant it's fetched, before the rest of
                this prediction runs. Lets a caller kick off something
                slow (e.g. a live simulation, see backend/app.py) on
                that exact same weather, running concurrently with the
                rest of this function rather than waiting for it to
                finish first. Never called for ARIMA, which has no
                weather-fetch step to hook into.

    Returns a ForecastResult(power, weather):
      power: pandas Series of hourly predicted-watt values (24 per
                 day in range), indexed by timestamp, already scaled
                 by quantity.
      weather: DataFrame indexed by time with shortwave_radiation_wm2
                 and temperature_2m_c for the same hours, or None for
                 ARIMA (which never sees weather at all).
    """
    if model not in _DISPATCH:
        raise ValueError(f"Unknown model '{model}'. Choose from: {list(_DISPATCH)}")

    return _DISPATCH[model](part_id, start_date, end_date, quantity, latitude=latitude, longitude=longitude,
                             on_weather_fetched=on_weather_fetched)


def predict(part_id, start_date, end_date=None, model="random_forest", quantity=1,
            latitude=None, longitude=None):
    """
    Same as predict_with_weather(), but returns just the power Series
    for callers that don't need the weather context.
    """
    return predict_with_weather(
        part_id, start_date, end_date=end_date, model=model, quantity=quantity,
        latitude=latitude, longitude=longitude,
    ).power
