"""
Fits an independent SARIMA model per representative panel (small/medium/
large rated capacity) and evaluates each against a held-out chronological
test window.

ARIMA only ever sees a panel's own past power values, no weather input,
so it's the classical, weather-blind baseline in the 3-model
comparison. It can't generalize across panels (no panel-spec input) and
can't react to a weather forecast, so it's not part of the deployed app;
it exists to show how much a weather-aware model (Random Forest, LSTM)
gains over a purely historical extrapolation.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import joblib
import pandas as pd
import pmdarima as pm

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from evaluation.metrics import evaluate

parser = argparse.ArgumentParser(description="Fit SARIMA models for the 3 representative panels.")
parser.add_argument("--force", action="store_true",
                     help="Refit every panel even if a saved model already exists")
args = parser.parse_args()

base_dir = os.path.dirname(__file__)
data_path = os.path.join(base_dir, "..", "data", "processed", "arima_panels.parquet")
models_dir = os.path.join(base_dir, "..", "models", "arima")
results_path = os.path.join(base_dir, "..", "output", "tables", "arima_results.csv")

os.makedirs(models_dir, exist_ok=True)
os.makedirs(os.path.dirname(results_path), exist_ok=True)

TEST_HOURS = 24 * 30  # last 30 days held out for testing
SEASONAL_PERIOD = 24  # daily cycle

# Seasonal SARIMAX fitting on the full ~95,712-hour (10.9-year) history
# was getting OOM-killed. its internal state-space representation
# gets memory-hungry with long series, especially for the more complex
# candidate orders auto_arima tries. The daily cycle repeats constantly
# though, so it doesn't need over a decade of data to learn it. The
# most recent 2 years (thousands of repetitions of the daily pattern)
# is plenty, and cuts the training set by ~5.5x.
TRAIN_YEARS = 2
TRAIN_HOURS = 24 * 365 * TRAIN_YEARS

df = pd.read_parquet(data_path)
df["output_power_w"] = df["output_power_w"].clip(lower=0)

results = []

for part_id, panel_df in df.groupby("part_id"):

    panel_df = panel_df.sort_values("time").reset_index(drop=True)
    label = panel_df["model_name"].iloc[0]

    series = panel_df["output_power_w"]
    test = series.iloc[-TEST_HOURS:]
    train = series.iloc[-(TEST_HOURS + TRAIN_HOURS):-TEST_HOURS]

    time_col = panel_df["time"]
    train_time = time_col.iloc[-(TEST_HOURS + TRAIN_HOURS):-TEST_HOURS]
    test_time = time_col.iloc[-TEST_HOURS:]

    model_path = os.path.join(models_dir, f"{label}.pkl")
    meta_path = os.path.join(models_dir, f"{label}_meta.json")

    if os.path.isfile(model_path) and not args.force:
        # Already fitted in an earlier run (fitting is the expensive
        # part, ~10-12 min per panel). Just reload it and recompute
        # metrics/metadata instead of refitting from scratch. Pass
        # --force (or delete the .pkl) to refit anyway.
        print(f"\n{label}: already fitted, loading {model_path}")
        model = joblib.load(model_path)

    else:
        if args.force and os.path.isfile(model_path):
            print(f"\n{label}: --force given, refitting despite existing model")
        print(f"\n{'=' * 60}")
        print(f"Fitting ARIMA for {label} ({part_id})")
        print(f"{'=' * 60}")
        print(f"Train: {len(train):,} hours, Test: {len(test):,} hours")

        model = pm.auto_arima(
            train,
            seasonal=True,
            m=SEASONAL_PERIOD,
            max_p=3, max_q=3, max_P=1, max_Q=1,
            stepwise=True,
            suppress_warnings=True,
            error_action="ignore",
            trace=True,
        )

        print(f"Selected order: {model.order}, seasonal order: {model.seasonal_order}")

        joblib.dump(model, model_path)
        print(f"Saved: {model_path}")

    forecast = model.predict(n_periods=len(test))

    metrics = evaluate(test.values, forecast)
    metrics["panel"] = label
    metrics["part_id"] = part_id
    results.append(metrics)

    print(
        f"MAE={metrics['MAE']:.3f} W  RMSE={metrics['RMSE']:.3f} W  "
        f"MAPE={metrics['MAPE']:.2f}%  R2={metrics['R2']:.4f}"
    )

    # predict(n_periods=N) always continues from wherever this model's
    # training data ends. Recording that timestamp explicitly means
    # inference code never has to guess or hardcode it.
    meta = {
        "part_id": part_id,
        "panel": label,
        "seasonal_period": SEASONAL_PERIOD,
        "order": list(model.order),
        "seasonal_order": list(model.seasonal_order),
        "train_start": str(train_time.iloc[0]),
        "train_end": str(train_time.iloc[-1]),
        "test_start": str(test_time.iloc[0]),
        "test_end": str(test_time.iloc[-1]),
        "metrics": {k: metrics[k] for k in ("MAE", "RMSE", "MAPE", "R2")},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Saved: {meta_path}")

results_df = pd.DataFrame(results)[["panel", "part_id", "MAE", "RMSE", "MAPE", "R2"]]

print(f"\n{'=' * 60}")
print("Summary")
print(f"{'=' * 60}")
print(results_df.to_string(index=False))

results_df.to_csv(results_path, index=False)
print(f"\nSaved: {results_path}")
