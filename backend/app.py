
#!/usr/bin/env python3
"""
Flask backend for the forecasting web dashboard: the browser-facing
equivalent of forecast_cli.py. Both are thin wrappers around the same
common/inference.py functions; this file's only job is turning HTTP
requests into calls against that shared, standalone module and JSON
back out. No forecasting logic lives here.

Run with:
    python backend/app.py
then open http://127.0.0.1:5000
"""

import csv
import logging
import os
import sqlite3
import sys
import traceback

# backend/ is a subdirectory, so common/ (at the project root) isn't on
# sys.path the way it is for forecast_cli.py, which lives at the root
# and gets its own directory added automatically. Add the root explicitly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, jsonify, render_template, request

from common.dataset import EXCLUDED_PART_IDS
from common.inference import ARIMA_PANELS, get_panel_specs, predict_with_weather

STC_IRRADIANCE_WM2 = 1000

PANEL_DB = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "panel_cache.db")
TABLES_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "tables")

# ARIMA's representative panel shown in the model performance view.
# It's fit per-panel (see ARIMA_PANELS in common/inference.py), so
# there's no single set of metrics for it the way RF/LSTM have one
# model covering every panel. AS_6M30_HC_320W is the mid-size one.
ARIMA_REPRESENTATIVE_PANEL = "AS_6M30_HC_320W"

# INFO so the inference log below actually reaches the console (see
# api_forecast()). This is meant to be visible on the terminal
# during a live demo, not just written to a file nobody's watching.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# debug=True (see the bottom of this file) makes Flask re-raise any
# exception not caught inside a route instead of running it through
# the @app.errorhandler()s below. Werkzeug's interactive debugger
# then renders the full HTML traceback page as the response itself,
# which is exactly the raw-error-in-the-UI bug this file exists to
# prevent. Forcing this off is what actually makes the error handlers
# below apply unconditionally, in debug mode or not.
app.config["PROPAGATE_EXCEPTIONS"] = False


def _load_model_performance():
    """
    Reads the real evaluation metrics each training script wrote to
    output/tables/*.csv, never hardcoded, so this always reflects
    whatever the models were last actually scored at.
    """
    with open(os.path.join(TABLES_DIR, "random_forest_results.csv"), newline="") as f:
        rf = next(csv.DictReader(f))
    with open(os.path.join(TABLES_DIR, "lstm_results.csv"), newline="") as f:
        lstm = next(csv.DictReader(f))
    with open(os.path.join(TABLES_DIR, "arima_results.csv"), newline="") as f:
        arima_rows = {row["panel"]: row for row in csv.DictReader(f)}
    arima = arima_rows[ARIMA_REPRESENTATIVE_PANEL]

    def metrics(row):
        return {k: float(row[k]) for k in ("MAE", "RMSE", "MAPE", "R2")}

    return {
        "random_forest": metrics(rf),
        "lstm": metrics(lstm),
        "arima": dict(metrics(arima), representative_panel=ARIMA_REPRESENTATIVE_PANEL),
    }


# Read once at startup rather than on every request. These files
# only change when a model is retrained, which requires restarting
# this server to pick up new model weights anyway.
MODEL_PERFORMANCE = _load_model_performance()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/panels")
def api_panels():
    # Excluded (AzurSpace bare research cells) are left out entirely:
    # they can never be forecast, so there's nothing useful a picker
    # could do with them beyond confusing the user (see
    # common/dataset.py for why they're excluded from training).
    try:
        conn = sqlite3.connect(PANEL_DB)
        rows = conn.execute(
            "SELECT part_id, manufacturer, model_name, pmp FROM panels ORDER BY manufacturer, pmp"
        ).fetchall()
        conn.close()
    except Exception:
        app.logger.error("Unexpected error in /api/panels: %s", traceback.format_exc())
        return jsonify({"error": "Couldn't load the panel catalog right now. Please try again."}), 500

    panels = [
        {
            "part_id": part_id,
            "manufacturer": manufacturer,
            "model_name": model_name,
            "pmp": pmp,
            "arima_supported": part_id in ARIMA_PANELS,
        }
        for part_id, manufacturer, model_name, pmp in rows
        if part_id not in EXCLUDED_PART_IDS
    ]
    return jsonify(panels)


@app.route("/api/forecast")
def api_forecast():
    part_id = request.args.get("part_id")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")  # optional, single day when omitted
    model = request.args.get("model", "random_forest")
    quantity = request.args.get("quantity", 1, type=int)
    latitude = request.args.get("lat", type=float)
    longitude = request.args.get("lon", type=float)

    if not part_id or not start_date:
        return jsonify({"error": "part_id and start_date are required"}), 400

    try:
        power, weather = predict_with_weather(
            part_id, start_date, end_date=end_date, model=model, quantity=quantity,
            latitude=latitude, longitude=longitude,
        )
        specs = get_panel_specs(part_id)
    except (ValueError, NotImplementedError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception:
        # Anything else (a weather-API timeout, a connection error, a
        # bug) must still come back as a clean, generic JSON message,
        # never the raw exception text. The real detail (full
        # traceback) goes to the server console only, where it's
        # useful for debugging, not to whoever's looking at the UI.
        app.logger.error("Unexpected error in /api/forecast: %s", traceback.format_exc())
        return jsonify({"error": "Couldn't generate a forecast right now. Please try again."}), 500

    power_series = [
        {"time": ts.isoformat(), "watts": float(w)} for ts, w in power.items()
    ]
    weather_series = None
    if weather is not None:
        weather_series = [
            {
                "time": ts.isoformat(),
                "irradiance_wm2": float(row["shortwave_radiation_wm2"]),
                "temperature_c": float(row["temperature_2m_c"]),
            }
            for ts, row in weather.iterrows()
        ]

    # Prints the actual per-hour prediction, not just a summary, so
    # the raw model output can be shown straight off the server
    # console (e.g. during a live presentation) as evidence of what
    # the model actually produced, not just what the UI renders.
    peak = max(power_series, key=lambda row: row["watts"])
    total_kwh = sum(row["watts"] for row in power_series) / 1000
    log_lines = [
        "",
        "=" * 72,
        f"INFERENCE  panel={part_id}  model={model}  quantity={quantity}",
        f"range={start_date} to {end_date or start_date}"
        + (f"  location=({latitude}, {longitude})" if latitude is not None and longitude is not None else "  location=default site"),
        "-" * 72,
    ]
    log_lines.extend(f"  {row['time']}   {row['watts']:>10.2f} W" for row in power_series)
    log_lines.append("-" * 72)
    log_lines.append(
        f"peak={peak['watts']:.2f} W at {peak['time']}   "
        f"total={total_kwh:.2f} kWh   n={len(power_series)} hours"
    )
    log_lines.append("=" * 72)
    app.logger.info("\n".join(log_lines))

    return jsonify({
        "part_id": part_id,
        "start_date": start_date,
        "end_date": end_date or start_date,
        "model": model,
        "quantity": quantity,
        "used_location": (
            {"latitude": latitude, "longitude": longitude}
            if latitude is not None and longitude is not None and weather is not None
            else None
        ),
        "power": power_series,
        "weather": weather_series,
        "specs": specs,
        "rated_total_watts": specs["pmp"] * quantity,
        "stc_irradiance_wm2": STC_IRRADIANCE_WM2,
    })


@app.route("/api/model-performance")
def api_model_performance():
    try:
        return jsonify(MODEL_PERFORMANCE)
    except Exception:
        app.logger.error("Unexpected error in /api/model-performance: %s", traceback.format_exc())
        return jsonify({"error": "Couldn't load model performance right now. Please try again."}), 500


# Belt-and-suspenders: every /api/ route above already catches its own
# exceptions, but this covers anything that isn't (a typo'd URL hitting
# Flask's default 404, a future route someone forgets to wrap) so a
# fetch() aimed at /api/* can never come back with an HTML error page
# instead of JSON. That's what actually broke the frontend before.
@app.errorhandler(404)
def handle_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return e


@app.errorhandler(500)
def handle_500(e):
    if request.path.startswith("/api/"):
        app.logger.error("Unhandled error: %s", traceback.format_exc())
        return jsonify({"error": "Unexpected server error."}), 500
    return e


if __name__ == "__main__":
    app.run(debug=True)
