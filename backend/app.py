
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
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor

# backend/ is a subdirectory, so common/ (at the project root) isn't on
# sys.path the way it is for forecast_cli.py, which lives at the root
# and gets its own directory added automatically. Add the root explicitly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, jsonify, render_template, request

from common.dataset import EXCLUDED_PART_IDS
from common.inference import ARIMA_PANELS, get_panel_specs, predict_with_weather
from common.simulation import run_simulation
from common.weather_api import get_forecast

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

# ML predictions are independent per request and cheap (well under a
# second once weather's in hand), so a handful of workers lets several
# run at once. Every simulation call shares one cached MATLAB engine
# (see common/simulation.py), and a single engine session isn't safe
# for concurrent calls from multiple threads, so that pool is capped
# at 1, a queue enforcing correctness, not a resource limit.
ml_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ml")
sim_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim")

# job_id -> {"ml_future", "sim_state", request params..., "created_at"}.
# No expiry/reaper: fine at this project's scale (a handful of users at
# most), but an abandoned job (started, never polled) does linger here
# and its futures still run to completion rather than being cancelled.
_jobs = {}
_jobs_lock = threading.Lock()


def _build_forecast_payload(part_id, start_date, end_date, model, quantity, latitude, longitude,
                             power, weather, specs, simulated=None):
    """
    Shared by the synchronous /api/forecast and the job-based
    /api/forecast/jobs/<id>, so both ever return exactly the same
    shape, just with "simulated" present or absent. Also logs the raw
    hourly prediction to the console either way (see api_forecast()).
    """
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
    simulated_series = None
    if simulated is not None:
        simulated_series = [
            {"time": ts.isoformat(), "watts": float(w)} for ts, w in simulated.items()
        ]

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
    if simulated_series is not None:
        sim_total = sum(row["watts"] for row in simulated_series) / 1000
        log_lines.append(f"simulated total={sim_total:.2f} kWh (ground truth, same weather)")
    log_lines.append("=" * 72)
    app.logger.info("\n".join(log_lines))

    return {
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
        "simulated": simulated_series,
        "specs": specs,
        "rated_total_watts": specs["pmp"] * quantity,
        "stc_irradiance_wm2": STC_IRRADIANCE_WM2,
    }


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

    return jsonify(_build_forecast_payload(
        part_id, start_date, end_date, model, quantity, latitude, longitude, power, weather, specs,
    ))


@app.route("/api/forecast/jobs", methods=["POST"])
def start_forecast_job():
    """
    Kicks off the ML prediction and the live simulation comparison
    together, running concurrently rather than one waiting on the
    other, and returns immediately with a job id to poll (see
    forecast_job_status()). A synchronous request here would otherwise
    block on both a MATLAB engine call (cold: ~25-30s just to start,
    see common/simulation.py) and the ML prediction, which is exactly
    the kind of long-held-open request that breaks browsers and
    reverse proxies.
    """
    part_id = request.args.get("part_id")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    model = request.args.get("model", "random_forest")
    quantity = request.args.get("quantity", 1, type=int)
    latitude = request.args.get("lat", type=float)
    longitude = request.args.get("lon", type=float)

    if not part_id or not start_date:
        return jsonify({"error": "part_id and start_date are required"}), 400

    job_id = uuid.uuid4().hex
    sim_state = {"future": None}

    # Fires from inside the ml worker thread the instant weather is
    # fetched (see common/inference.py's on_weather_fetched), so the
    # simulation starts on the exact same weather the model itself
    # used, and runs concurrently with the rest of that same
    # prediction rather than waiting for it to finish first.
    def on_weather_fetched(weather_df):
        sim_state["future"] = sim_executor.submit(run_simulation, part_id, weather_df)

    def run_ml():
        return predict_with_weather(
            part_id, start_date, end_date=end_date, model=model, quantity=quantity,
            latitude=latitude, longitude=longitude, on_weather_fetched=on_weather_fetched,
        )

    ml_future = ml_executor.submit(run_ml)

    # ARIMA never calls on_weather_fetched (no weather-fetch step to
    # hook into), but the comparison matters most for exactly this
    # model (see forecast_cli.py), so weather gets fetched
    # independently here, off the request thread, purely for this
    # comparison rather than fed into ARIMA's own prediction.
    if model == "arima":
        def fetch_and_simulate_arima():
            weather_df = get_forecast(start_date, end_date, latitude=latitude, longitude=longitude)
            return run_simulation(part_id, weather_df)

        sim_state["future"] = sim_executor.submit(fetch_and_simulate_arima)

    with _jobs_lock:
        _jobs[job_id] = {
            "ml_future": ml_future,
            "sim_state": sim_state,
            "part_id": part_id,
            "start_date": start_date,
            "end_date": end_date,
            "model": model,
            "quantity": quantity,
            "latitude": latitude,
            "longitude": longitude,
            "created_at": time.time(),
        }

    return jsonify({"job_id": job_id})


@app.route("/api/forecast/jobs/<job_id>")
def forecast_job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"error": "Unknown or expired job"}), 404

    ml_future = job["ml_future"]
    sim_future = job["sim_state"]["future"]

    # sim_future starts out None for random_forest/lstm until
    # on_weather_fetched actually fires inside the ml worker thread, so
    # "no future yet" still means pending, not an error.
    if not ml_future.done() or sim_future is None or not sim_future.done():
        return jsonify({"status": "pending"})

    try:
        power, weather = ml_future.result()
        specs = get_panel_specs(job["part_id"])
    except (ValueError, NotImplementedError) as e:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        return jsonify({"status": "error", "error": str(e)}), 400
    except Exception:
        app.logger.error("Unexpected error in forecast job %s: %s", job_id, traceback.format_exc())
        with _jobs_lock:
            _jobs.pop(job_id, None)
        return jsonify({"status": "error", "error": "Couldn't generate a forecast right now. Please try again."}), 500

    simulated = None
    try:
        sim_result = sim_future.result()
        if sim_result is not None:
            simulated = sim_result * job["quantity"]
    except Exception:
        # The model forecast above is still valid on its own; a failed
        # simulation degrades to just not having a comparison, not a
        # failed forecast.
        app.logger.error("Unexpected error running simulation for job %s: %s", job_id, traceback.format_exc())

    payload = _build_forecast_payload(
        job["part_id"], job["start_date"], job["end_date"], job["model"], job["quantity"],
        job["latitude"], job["longitude"], power, weather, specs, simulated,
    )

    with _jobs_lock:
        _jobs.pop(job_id, None)

    return jsonify(dict(payload, status="done"))


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
