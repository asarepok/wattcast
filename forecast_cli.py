#!/usr/bin/env python3
"""
Command-line interface for getting a power forecast, the terminal-based
equivalent of the web app. Wraps common/inference.py.

Run with no arguments for an interactive prompt, or pass flags
directly for scripting:

    python forecast_cli.py
    python forecast_cli.py --panel AS_6M30_HC_320W --date 2026-08-20 --model arima
    python forecast_cli.py --list-panels --search amerisolar
    python forecast_cli.py --help
"""

import argparse
import os
import sqlite3

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

from common.dataset import EXCLUDED_PART_IDS
from common.inference import get_panel_specs, predict_with_weather
from common.simulation import run_simulation
from common.weather_api import get_forecast

# Rated Pmax on any datasheet is measured at Standard Test Conditions:
# 1000 W/m^2 irradiance, 25C cell temperature. Shown as a reference line
# on the irradiance panel so it's clear why real-world output is usually
# well below the rated number: actual irradiance is rarely at 1000.
STC_IRRADIANCE_WM2 = 1000

FIGURES_DIR = os.path.join(os.path.dirname(__file__), "output", "figures")

PANEL_DB = os.path.join(os.path.dirname(__file__), "data", "raw", "panel_cache.db")


def resolve_part_id(panel):
    """Accepts either a full part_id (contains '|') or a bare model_name."""

    if "|" in panel:
        return panel

    conn = sqlite3.connect(PANEL_DB)
    rows = conn.execute(
        "SELECT part_id FROM panels WHERE model_name = ? COLLATE NOCASE",
        (panel,),
    ).fetchall()
    conn.close()

    if not rows:
        raise SystemExit(
            f"No panel found with model_name '{panel}'. Run with "
            f"--list-panels to see available panels, or pass a full part_id."
        )
    if len(rows) > 1:
        raise SystemExit(f"Multiple panels match '{panel}'. Use a full part_id instead.")

    return rows[0][0]


def list_panels(search=None):
    conn = sqlite3.connect(PANEL_DB)
    query = "SELECT manufacturer, model_name, part_id, pmp FROM panels"
    params = ()
    if search:
        query += " WHERE manufacturer LIKE ? OR model_name LIKE ?"
        params = (f"%{search}%", f"%{search}%")
    query += " ORDER BY manufacturer, model_name"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    if not rows:
        print("No matching panels.")
        return

    print(f"{'Manufacturer':<20} {'Model':<30} {'Rated (W)':>10}")
    print("-" * 62)
    for manufacturer, model_name, part_id, pmp in rows:
        flag = "  [excluded]" if part_id in EXCLUDED_PART_IDS else ""
        print(f"{manufacturer:<20} {model_name:<30} {pmp:>10.1f}{flag}")
    print(f"\n{len(rows)} panel(s). ARIMA only supports the 3 representative "
          f"panels (FS_390_PLUS, AS_6M30_HC_320W, TP7F78M_H_590). Random "
          f"Forest and LSTM support every panel except the 2 marked "
          f"[excluded] (AzurSpace bare research cells, not real modules,"
          f" dropped from training entirely).")


def prompt_for_panel():
    # Nobody has 235 part codes memorized. Browse by manufacturer first
    # (only 20 of those, fits on one screen) then by model within it,
    # sorted by rated wattage rather than model code, since a user is
    # far more likely to know "mine's about 350W" (it's printed right
    # on the panel) than an internal naming scheme. Free-text search is
    # still available for anyone who does know something to type.
    conn = sqlite3.connect(PANEL_DB)

    manufacturers = [r[0] for r in conn.execute(
        "SELECT DISTINCT manufacturer FROM panels ORDER BY manufacturer"
    ).fetchall()]

    print("\nWhich manufacturer made your panel?")
    for i, m in enumerate(manufacturers, 1):
        print(f"  [{i:2d}] {m}")
    print("  (Or type part of a manufacturer/model name to search instead.)")

    rows = []
    while not rows:
        choice = input("Choice: ").strip()

        if choice.isdigit() and 1 <= int(choice) <= len(manufacturers):
            manufacturer = manufacturers[int(choice) - 1]
            rows = conn.execute(
                "SELECT part_id, manufacturer, model_name, pmp FROM panels "
                "WHERE manufacturer = ? ORDER BY pmp",
                (manufacturer,),
            ).fetchall()
        elif choice:
            rows = conn.execute(
                "SELECT part_id, manufacturer, model_name, pmp FROM panels "
                "WHERE manufacturer LIKE ? OR model_name LIKE ? "
                "ORDER BY manufacturer, pmp",
                (f"%{choice}%", f"%{choice}%"),
            ).fetchall()
        else:
            print("Pick a number, or type something to search.")

        if not rows:
            print("No matches, try again.\n")

    if len(rows) == 1:
        part_id, manufacturer, model_name, pmp = rows[0]
        print(f"Using: {manufacturer} {model_name} ({pmp:.1f} W)\n")
        conn.close()
        return part_id

    print(f"\n{len(rows)} models. If you know roughly how many watts your panel is rated for, look for that:")
    for i, (part_id, manufacturer, model_name, pmp) in enumerate(rows, 1):
        print(f"  [{i:2d}] {manufacturer} {model_name}  ({pmp:.1f} W)")

    while True:
        pick = input(f"Pick a number (1-{len(rows)}): ").strip()
        if pick.isdigit() and 1 <= int(pick) <= len(rows):
            conn.close()
            return rows[int(pick) - 1][0]
        print("Invalid choice, try again.")


def prompt_for_date():
    while True:
        raw = input("Target date (YYYY-MM-DD): ").strip()
        try:
            return str(pd.Timestamp(raw).date())
        except (ValueError, TypeError):
            print("Couldn't parse that as a date, try e.g. 2026-08-20.\n")


def prompt_for_model():
    print("\nWhich model?")
    print("  [1] ARIMA")
    print("  [2] Random Forest (default)")
    print("  [3] LSTM")
    choice = input("Choice (Enter for default): ").strip()
    return {"1": "arima", "3": "lstm"}.get(choice, "random_forest")


def prompt_for_quantity():
    raw = input("How many panels do you own? (Enter for 1): ").strip()
    if not raw:
        return 1
    try:
        return int(raw)
    except ValueError:
        print("Not a number, defaulting to 1.")
        return 1


def print_forecast(part_id, date, model, quantity, forecast, weather, simulated=None):
    print(f"\nForecast for {part_id} x{quantity} on {date} ({model})")

    if weather is None:
        # ARIMA: no weather involved at all, power only, and nothing to
        # run a matching simulation against either.
        print("-" * 50)
        for ts, watts in forecast.items():
            print(f"  {ts.strftime('%H:%M')}  {watts:8.1f} W")
        print("-" * 50)
    elif simulated is None:
        header = f"  {'Time':<6} {'Power (W)':>10} {'Irradiance (W/m2)':>19} {'Temp (C)':>9}"
        print("-" * len(header))
        print(header)
        print("-" * len(header))
        for ts, watts in forecast.items():
            irr = weather.loc[ts, "shortwave_radiation_wm2"]
            temp = weather.loc[ts, "temperature_2m_c"]
            print(f"  {ts.strftime('%H:%M'):<6} {watts:>10.1f} {irr:>19.1f} {temp:>9.1f}")
        print("-" * len(header))
    else:
        header = (f"  {'Time':<6} {'Power (W)':>10} {'Simulated (W)':>14} "
                   f"{'Irradiance (W/m2)':>19} {'Temp (C)':>9}")
        print("-" * len(header))
        print(header)
        print("-" * len(header))
        for ts, watts in forecast.items():
            sim_watts = simulated.loc[ts]
            irr = weather.loc[ts, "shortwave_radiation_wm2"]
            temp = weather.loc[ts, "temperature_2m_c"]
            print(f"  {ts.strftime('%H:%M'):<6} {watts:>10.1f} {sim_watts:>14.1f} "
                  f"{irr:>19.1f} {temp:>9.1f}")
        print("-" * len(header))

    print(f"  Daily total (model):      {forecast.sum() / 1000:.2f} kWh")
    if simulated is not None:
        sim_total = simulated.sum() / 1000
        model_total = forecast.sum() / 1000
        diff_pct = (model_total - sim_total) / sim_total * 100 if sim_total else 0.0
        print(f"  Daily total (simulated):  {sim_total:.2f} kWh")
        print(f"  Difference:               {model_total - sim_total:+.2f} kWh ({diff_pct:+.1f}%)")


def print_panel_specs(specs, quantity, forecast):
    # Printed after inferencing, not before. This is meant as a
    # baseline to check the forecast against (how does peak predicted
    # output compare to what the panel is actually rated for), not
    # just a spec dump.
    rated_total = specs["pmp"] * quantity
    peak = float(forecast.max())

    print("\nPanel specs (baseline for comparison):")
    print(f"  Manufacturer:     {specs['manufacturer']}")
    print(f"  Model:            {specs['model_name']}")
    print(f"  Rated Pmax:       {specs['pmp']:.1f} W  (x{quantity} = {rated_total:.1f} W)")
    print(f"                    (measured at Standard Test Conditions: "
          f"{STC_IRRADIANCE_WM2} W/m² irradiance, 25°C cell temperature)")
    print(f"  Rated Voc:        {specs['voc']:.1f} V")
    print(f"  Rated Isc:        {specs['isc']:.2f} A")
    print(f"  Rated Vmp:        {specs['vmp']:.1f} V")
    print(f"  Rated Imp:        {specs['imp']:.2f} A")
    print(f"  Cells in series:  {int(specs['n_series'])}")
    print(f"  Parallel strings: {int(specs['n_parallel'])}")
    print(f"\n  Peak predicted:   {peak:.1f} W of {rated_total:.1f} W rated ({peak / rated_total * 100:.1f}%)")


def plot_forecast(specs, date, model, quantity, forecast, weather, simulated=None):
    rated_total = specs["pmp"] * quantity
    peak_idx = int(forecast.values.argmax())
    peak_time = forecast.index[peak_idx]
    peak_val = float(forecast.values[peak_idx])

    # Power, irradiance, and temperature are 3 different units. Never
    # put them on one axis with a second y-scale (a dual-axis chart is
    # the single most common charting mistake, and different-scale
    # measures like these should always be small multiples instead).
    if weather is not None:
        fig, (ax_power, ax_irr, ax_temp) = plt.subplots(3, 1, figsize=(9, 10), sharex=True)
    else:
        fig, ax_power = plt.subplots(figsize=(9, 5))

    ax_power.plot(forecast.index, forecast.values, color="#2a78d6", linewidth=2, label="Predicted power")
    if simulated is not None:
        ax_power.plot(simulated.index, simulated.values, color="#e34948", linewidth=1.5,
                      linestyle=":", label="Simulated (ground truth)")
    ax_power.axhline(rated_total, color="#71706b", linestyle="--", linewidth=1.5,
                      label=f"Rated capacity ({rated_total:.0f} W)")
    ax_power.annotate(f"{peak_val:.0f} W", xy=(peak_time, peak_val), xytext=(0, 10),
                       textcoords="offset points", ha="center", fontsize=9, color="#0b0b0b")
    ax_power.set_ylabel("Power (W)")
    ax_power.grid(True, alpha=0.25)
    ax_power.legend(frameon=False)

    if weather is not None:
        ax_irr.plot(weather.index, weather["shortwave_radiation_wm2"],
                    color="#eda100", linewidth=2, label="Irradiance")
        ax_irr.axhline(STC_IRRADIANCE_WM2, color="#71706b", linestyle="--", linewidth=1.5,
                        label=f"{STC_IRRADIANCE_WM2} W/m² (Standard Test Conditions, rated Pmax basis)")
        ax_irr.set_ylabel("Irradiance (W/m²)")
        ax_irr.grid(True, alpha=0.25)
        ax_irr.legend(frameon=False)

        ax_temp.plot(weather.index, weather["temperature_2m_c"], color="#e34948", linewidth=2)
        ax_temp.set_ylabel("Temperature (°C)")
        ax_temp.set_xlabel("Hour")
        ax_temp.grid(True, alpha=0.25)

        ax_temp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    else:
        ax_power.set_xlabel("Hour")
        ax_power.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))

    fig.suptitle(f"{specs['manufacturer']} {specs['model_name']} x{quantity}, {date} ({model})")
    fig.autofmt_xdate()
    fig.tight_layout()

    os.makedirs(FIGURES_DIR, exist_ok=True)
    safe_name = specs["model_name"].replace("/", "_")
    out_path = os.path.join(FIGURES_DIR, f"forecast_{safe_name}_{date}_{model}.png")
    fig.savefig(out_path, dpi=150)
    print(f"\nSaved plot: {out_path}")

    plt.show()


def main():
    parser = argparse.ArgumentParser(
        description="Get a solar power forecast from the command line. "
                     "Run with no arguments for an interactive prompt.",
    )
    parser.add_argument("--panel", help="Panel model_name (e.g. AS_6M30_HC_320W) or full part_id")
    parser.add_argument("--date", help="Target date, e.g. 2026-08-20")
    parser.add_argument("--model", choices=["arima", "random_forest", "lstm"],
                         help="Which model to use (default: random_forest)")
    parser.add_argument("--quantity", type=int, help="Number of panels owned (default: 1)")
    parser.add_argument("--list-panels", action="store_true", help="List available panels and exit")
    parser.add_argument("--search", help="Filter --list-panels by manufacturer/model substring")

    args = parser.parse_args()

    if args.list_panels:
        list_panels(args.search)
        return

    if args.panel is None and args.date is None:
        print("No arguments given, let's walk through it. (Run with --help to see flags for scripting.)\n")
        part_id = prompt_for_panel()
        date = prompt_for_date()
        model = prompt_for_model()
        quantity = prompt_for_quantity()
    else:
        if not args.panel or not args.date:
            parser.error("--panel and --date must be given together (or run with no arguments for interactive mode)")
        part_id = resolve_part_id(args.panel)
        date = args.date
        model = args.model or "random_forest"
        quantity = args.quantity or 1

    # Progress visibility lives here in the CLI, not inside common/
    # inference.py or common/weather_api.py. Those are meant to stay
    # standalone, side-effect-free modules that other things (a future
    # Flask backend, etc.) can import without inheriting print()s aimed
    # at a terminal user.
    if model == "arima":
        print(f"\nRunning ARIMA inference for {date} (no weather involved, pure extrapolation)...")
    else:
        print(f"\nFetching live weather forecast and running {model} inference for {date}...")

    try:
        forecast, weather = predict_with_weather(part_id, date, model=model, quantity=quantity)
    except (ValueError, NotImplementedError) as e:
        raise SystemExit(f"\nError: {e}")

    # Every forecast, regardless of model, gets checked against the
    # actual Simscape Electrical simulation on the real weather for that
    # date. ARIMA never takes weather as an input, but that's exactly
    # why this comparison matters most for it: it shows what the panel
    # actually produced next to ARIMA's weather-blind guess. So weather
    # gets fetched here too when the model itself didn't already fetch
    # it, purely for this comparison, not fed into ARIMA's own
    # prediction. A first simulation run pays MATLAB's own startup cost
    # (engine + model load, under a minute); it's cached in
    # common/simulation.py, so any later forecast in this same process
    # is fast.
    if weather is None:
        try:
            # .set_index("time") to match predict_with_weather()'s own
            # returned weather shape for RF/LSTM, since print_forecast()
            # below does weather.loc[timestamp, column] either way.
            weather = get_forecast(date, None).set_index("time")
        except Exception as e:
            print(f"Couldn't fetch weather for the simulation comparison: {e}")

    simulated = None
    if weather is not None:
        print(f"Running live Simulink simulation on the same weather for comparison "
              f"(this can take up to a minute the first time)...")
        try:
            simulated = run_simulation(part_id, weather) * quantity
        except Exception as e:
            print(f"Couldn't run the simulation comparison: {e}")
            print("Continuing with just the model's forecast.")

    print_forecast(part_id, date, model, quantity, forecast, weather, simulated)

    try:
        specs = get_panel_specs(part_id)
    except ValueError:
        specs = None  # predict_with_weather() already succeeded, so this shouldn't happen; skip gracefully if it somehow does

    if specs:
        print_panel_specs(specs, quantity, forecast)
        plot_forecast(specs, date, model, quantity, forecast, weather, simulated)


if __name__ == "__main__":
    main()
