# Wattcast: Solar Output Forecasting Using Time Series and Machine Learning

Forecasts hourly solar panel power output using live weather data and one of
three models: Random Forest, LSTM, or ARIMA. Includes a command-line tool, a
web dashboard ("Wattcast"), and the full pipeline behind them: MATLAB/Simulink
panel simulation, data preprocessing, model training, and evaluation.

## Screenshots

**Forecast dashboard**, live weather behind an hourly power forecast for a
chosen panel, quantity, location, and date range:

![Wattcast forecast dashboard](docs/screenshots/dashboard.png)

**Model performance comparison**, real evaluation metrics for all three
engines, plus what each engine's typical error means for the current
forecast:

![Model performance modal](docs/screenshots/model-performance.png)

**Simulink panel simulation model**, the Simscape Electrical circuit behind
every panel's simulated hourly output (irradiance and temperature in,
current/voltage/power out):

![Simulink block diagram](docs/screenshots/simulink-model.png)

## How it fits together

```
Simulink panel simulation (simulink/)
    -> raw simulated power data (data/simulated/, gitignored)
    -> preprocessing (scripts/)
    -> training data (data/processed/, gitignored)
    -> model training (training/)
    -> trained models (models/, gitignored)
    -> shared inference layer (common/inference.py)
    -> forecast_cli.py  and  backend/ (the Wattcast web app)
```

`common/` is the one place both the CLI and the web app call into for an
actual forecast. Neither the CLI nor the backend duplicates any forecasting
logic; they're thin wrappers that turn a terminal argument or an HTTP request
into a call against `common/inference.py`.

## What's NOT in this repository

Three things are deliberately excluded via `.gitignore`, all regenerable from
what IS committed:

- `data/processed/` and `data/simulated/` (roughly 6 GB): pipeline-generated
  training data. Regenerate via `simulink/` and `scripts/` (see below).
- `models/` (roughly 23 GB): trained model weights. Regenerate via
  `training/` (see below). The Random Forest model alone is around 17 GB on
  disk once trained, since it's 200 trees grown to full depth on a 22M+ row
  dataset; loading it cold takes on the order of 15-20 seconds even after
  training, which is expected, not a bug.

`data/raw/` (under 6 MB: the panel spec database and the historical weather
export) IS committed, since both are small and needed to run anything at all
without regenerating them from MATLAB first.

## Setup

Requires Python 3.12 (this is what the project was built and tested against).

```
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

MATLAB with Simulink is only needed if you want to regenerate the panel
cache or the simulated training data yourself. Running the CLI or the web
app against already-trained models needs nothing beyond the Python
environment above. Requires **MATLAB R2025a or R2025b**: `simulink/PVOutputSimulation2025a.slx`
is built for R2025a, `simulink/PVOutputSimulation.slx` for R2025b. The `.m`
scripts below load `PVOutputSimulation2025a.slx` by default; on R2025b,
point them at `PVOutputSimulation.slx` instead. For any other MATLAB
version, contact hello@asare.me for a compatible model file.

## Reproducing the data pipeline

Only needed if `data/raw/panel_cache.db`, `data/processed/`, or
`data/simulated/` don't already exist. Run in this order, each step depends
on the one before it:

1. `simulink/generate_panel_cache.m` characterizes every Solar Cell part in
   the Simscape Electrical collection (temperature/irradiance curves, IV
   curve, rated specs) and writes them to `data/raw/panel_cache.db`. This
   has to run first: step 2 reads the panel list straight out of this
   database, it doesn't work from the Simulink part collection directly.
   `data/raw/panel_cache.db` is already committed to this repo, so this
   step is only needed if you've deleted it or want to regenerate it
   (e.g. after the part collection changes).
2. `simulink/generate_training_data_all_panels.m` reads `panel_cache.db`
   for the list of panels, loads `PVOutputSimulation2025a.slx`, and
   simulates every panel's hourly output against the historical weather
   record (`data/raw/weather_data.csv`), writing the raw simulated dataset
   to `data/simulated/training_data_all_panels.csv` (large: several GB).
3. `scripts/preprocess_weather_data.py` cleans the raw Open-Meteo weather
   export (`data/raw/weather_data.csv`) and checks it for missing values,
   duplicate timestamps, gaps in the hourly sequence, and out-of-range
   readings.
4. `scripts/finalize_training_data.py` merges the simulated power data with
   the cleaned weather data and panel specs into the final training table
   used by every model.

## Training the models

Each script is independent and writes its own results:

```
python training/train_random_forest.py
python training/train_lstm.py
python training/train_arima.py           # add --force to refit even if a saved model exists
```

All three read `data/processed/` and write to `models/` plus a results CSV
in `output/tables/`. Random Forest and LSTM train on the full 235-panel
dataset; ARIMA fits a separate model per representative panel (small/medium/
large rated capacity) since it has no way to generalize across panels.

Real evaluation results from the last training run (see `output/tables/`):

| Model | MAE (W) | RMSE (W) | MAPE (%) | R2 |
|---|---|---|---|---|
| Random Forest | 0.11 | 0.27 | 0.72 | 0.99999 |
| LSTM | 1.33 | 2.11 | 5.85 | 0.99960 |
| ARIMA (avg across 3 panels) | 69.31 | 78.96 | 54.64 | 0.17663 |

R2 is shown to 5 decimal places specifically because Random Forest's real
value (0.9999935870843596) is not exactly 1.0, and rounding it to fewer
decimals would misleadingly suggest a perfect fit.

ARIMA trails the other two by a wide margin because it never sees weather;
it can only extrapolate a panel's own past output forward in time. It's kept
in the comparison specifically to show how much a weather-aware model gains
over a purely historical baseline. It's also the only one of the three not
wired into the deployed web app, for that same reason.

## Running the command-line tool

```
python forecast_cli.py                                            # interactive prompt
python forecast_cli.py --panel AS_6M30_HC_320W --date 2026-08-20 --model random_forest --quantity 12
python forecast_cli.py --list-panels --search amerisolar
python forecast_cli.py --help
```

Prints an hourly forecast table to the terminal and saves a plot (power,
irradiance, and temperature as small multiples) to `output/figures/`.

## Running the web app

```
python backend/app.py
```

Then open `http://127.0.0.1:5000`. The setup wizard walks through picking a
panel, quantity, and location (real GPS via the browser, or a city search,
both backed by free/keyless geocoding APIs), then shows a forecast dashboard
with live Open-Meteo weather behind it. Runs in Flask's debug server, which
is fine for local use or a demo but isn't meant for production deployment.

Every forecast request also prints the full hourly prediction to the server
console, useful for showing the raw model output alongside the dashboard
rather than just trusting the UI.

## Project layout

```
backend/            Flask web app + Wattcast dashboard (templates/static)
common/             Shared inference, dataset, and feature-engineering code
evaluation/         Shared metrics (MAE/RMSE/MAPE/R2) used by every training script
forecast_cli.py     Command-line forecast tool
scripts/            Data preprocessing (weather cleaning, final training table)
simulink/           MATLAB/Simulink panel simulation models
training/           One training script per model (random_forest, lstm, arima)
output/              Evaluation results (tables/) and saved forecast plots (figures/)
data/raw/           Small committed seed data (panel specs, historical weather)
data/processed/     Generated training data (gitignored, see "Reproducing the data pipeline")
data/simulated/     Raw Simulink simulation output (gitignored)
models/             Trained model weights (gitignored, see "Training the models")
```
