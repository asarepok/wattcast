"""
Runs the real Simscape Electrical circuit simulation for one panel
against a given weather record, on demand, for direct comparison against
a trained model's prediction on that exact same weather. Mirrors what
simulink/generate_training_data_all_panels.m does per panel, just for one
short on-demand run instead of the full historical sweep.

    from common.simulation import run_simulation
    run_simulation(part_id, weather_df)

weather_df must have the same shape common/weather_api.get_forecast()
returns: a 'time' column plus shortwave_radiation_wm2 and
temperature_2m_c, one row per hour. Returns a pandas Series of simulated
watts indexed by time, for one physical unit, not yet scaled by quantity
(same convention as common/inference.py's predict_with_weather).

Talks to MATLAB through matlab.engine (see requirements.txt: matlabengine,
pinned to the installed MATLAB version). Starting the engine and loading
the model together take on the order of 25-30 seconds; both are cached at
module level and reused for the life of the process, so only the actual
sim() call (a few seconds per day simulated) repeats per call.
"""

import os
import sqlite3

import matlab.engine
import numpy as np
import pandas as pd

SIMULINK_DIR = os.path.join(os.path.dirname(__file__), "..", "simulink")
PANEL_DB = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "panel_cache.db")

MODEL_NAME = "PVOutputSimulation2025a"
SOLAR_BLOCK = f"{MODEL_NAME}/Solar Cell"

_engine = None


def _get_engine():
    """
    Starts MATLAB and loads the model once per process (loading the
    model alone takes ~20s, on top of ~5s to start the engine itself),
    then reuses both for every subsequent call. Lazy rather than at
    import time, since importing this module shouldn't require MATLAB
    to already be running.
    """
    global _engine
    if _engine is None:
        _engine = matlab.engine.start_matlab()
        model_path = os.path.join(SIMULINK_DIR, MODEL_NAME + ".slx")
        _engine.eval(f"load_system('{model_path}');", nargout=0)
    return _engine


def _panel_lookup(part_id):
    """
    panel_cache.db's internal integer id, not part_id, is what
    temperature_characteristics is keyed on, so that has to be looked up
    first. Returns the per-panel temperature/vmp curve the model's 1-D
    Lookup Table block needs (see run_simulation()).
    """
    conn = sqlite3.connect(PANEL_DB)
    row = conn.execute("SELECT id FROM panels WHERE part_id = ?", (part_id,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError(f"Unknown part_id: '{part_id}'")
    panel_id = row[0]

    curve = conn.execute(
        "SELECT temperature_degC, vm_volts FROM temperature_characteristics "
        "WHERE panel_id = ? ORDER BY temperature_degC",
        (panel_id,),
    ).fetchall()
    conn.close()
    if not curve:
        raise ValueError(f"No temperature characteristics cached for '{part_id}'")

    temperature_points = [r[0] for r in curve]
    vmp_points = [r[1] for r in curve]
    return temperature_points, vmp_points


def run_simulation(part_id, weather_df):
    """
    Runs the real circuit simulation for one physical unit of part_id
    against weather_df, using the exact same weather a model prediction
    for the same request would have used.

    Every value pushed into the MATLAB base workspace here mirrors what
    simulink/generate_training_data_all_panels.m itself sets up before
    calling sim(), reverse-engineered by running it and reading the
    actual errors rather than assumed from the script alone. Two things
    aren't obvious from the script's own text:
      - StopTime in the model's Configuration Parameters is the
        expression (height(data)-1)*3600, so a base-workspace table
        literally named "data" with the right row count has to exist,
        not just the ghi_ts/temp_ts timeseries.
      - The 1-D Lookup Table block actually reads temperaturePoints and
        vmpPoints, not temperatureLookup/vmpLookup as the .m script's own
        comment implies. Both pairs are set here since something else in
        the model may still depend on the Lookup-suffixed pair.
    """
    eng = _get_engine()

    temperature_points, vmp_points = _panel_lookup(part_id)

    # common/weather_api.get_forecast() has "time" as a plain column, but
    # predict_with_weather()'s returned weather (what callers actually
    # have on hand, for the "same weather" comparison this exists for)
    # has already been through df.set_index("time"), so "time" is the
    # index there instead. Normalize so both shapes work.
    if "time" not in weather_df.columns:
        weather_df = weather_df.reset_index()
    weather_df = weather_df.sort_values("time").reset_index(drop=True)
    sim_time_sec = (weather_df["time"] - weather_df["time"].iloc[0]).dt.total_seconds()

    eng.workspace["tmpGhiData"] = matlab.double(weather_df["shortwave_radiation_wm2"].tolist())
    eng.workspace["tmpTempData"] = matlab.double(weather_df["temperature_2m_c"].tolist())
    eng.workspace["tmpSimTimeSec"] = matlab.double(sim_time_sec.tolist())
    eng.eval("ghi_ts = timeseries(tmpGhiData(:), tmpSimTimeSec(:));", nargout=0)
    eng.eval("temp_ts = timeseries(tmpTempData(:), tmpSimTimeSec(:));", nargout=0)
    eng.eval("data = table((1:numel(get(ghi_ts, 'Time')))');", nargout=0)

    eng.workspace["temperatureLookup"] = matlab.double(temperature_points)
    eng.workspace["vmpLookup"] = matlab.double(vmp_points)
    eng.workspace["temperaturePoints"] = matlab.double(temperature_points)
    eng.workspace["vmpPoints"] = matlab.double(vmp_points)

    eng.workspace["tmpPartId"] = part_id
    eng.eval(
        f'partrepo.collection.applyDataSet("{SOLAR_BLOCK}", "SimscapeElectrical", tmpPartId);',
        nargout=0,
    )

    eng.eval(f"tmpOut = sim('{MODEL_NAME}');", nargout=0)
    eng.eval("tmpSimPower = tmpOut.sim_power(:);", nargout=0)
    sim_power = np.array(eng.workspace["tmpSimPower"]).ravel()

    if len(sim_power) != len(weather_df):
        raise RuntimeError(
            f"Simulation produced {len(sim_power)} rows but weather_df has "
            f"{len(weather_df)}; something about the input didn't line up."
        )

    return pd.Series(sim_power, index=weather_df["time"])
