"""
Wraps Open-Meteo's forecast API: the live weather source used at
inference time, as opposed to the historical archive API used to build
the training dataset (see simulink/generate_training_data_all_panels.m).
Returns data in the exact same column names as the training schema (see
common/features.py's WEATHER_FEATURES), so it can be fed straight into
Random Forest / LSTM inference without any extra transformation.

Verified live against the real API (2026-08-15): start_date/end_date
lets us request one specific day's 24 hourly values directly rather
than "next N days from today" and slicing. Valid range covers roughly
3 months in the past through 16 days in the future (Open-Meteo blends
recent-past reanalysis with the actual forecast); requesting outside
that range returns a clean {"error": true, "reason": "..."} body rather
than raising an HTTP error, so it has to be checked explicitly.
"""

import pandas as pd
import requests

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Same site the training data was generated for (see data/raw/weather_data.csv).
# Used as the fallback whenever a caller doesn't supply its own coordinates
# (e.g. the CLI, or a browser that denied/lacks geolocation).
LATITUDE = 8.119508
LONGITUDE = -1.231842

# Open-Meteo's raw variable names -> our training schema's column names.
# Must match common/features.py's WEATHER_FEATURES exactly.
VARIABLES = {
    "temperature_2m": "temperature_2m_c",
    "shortwave_radiation": "shortwave_radiation_wm2",
    "relative_humidity_2m": "relative_humidity_pct",
    "cloud_cover": "cloud_cover_pct",
    "wind_speed_10m": "wind_speed_10m_kmh",
    "precipitation": "precipitation_mm",
    "dew_point_2m": "dew_point_2m_c",
}


def get_forecast(start_date, end_date=None, latitude=None, longitude=None):
    """
    Fetches hourly weather forecast for the requested site, from
    start_date through end_date inclusive. end_date defaults to
    start_date (a single day, 24 rows). LSTM inference passes a
    2-day range instead, since predicting the target day's first hour
    needs a 24-hour window reaching back into the previous day.

    start_date, end_date: any pandas-parseable date/string
    latitude, longitude: site to fetch weather for; defaults to the
        training site's coordinates (module-level LATITUDE/LONGITUDE)
        when either is omitted, e.g. a caller with no geolocation
        available.

    Returns a DataFrame with a 'time' column plus the same weather
    column names used in training (temperature_2m_c, etc.).
    Raises ValueError if the range falls outside Open-Meteo's currently
    available window (roughly 3 months back to 16 days ahead).
    """
    if end_date is None:
        end_date = start_date
    if latitude is None or longitude is None:
        latitude, longitude = LATITUDE, LONGITUDE

    start_str = str(pd.Timestamp(start_date).date())
    end_str = str(pd.Timestamp(end_date).date())

    response = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ",".join(VARIABLES),
            "start_date": start_str,
            "end_date": end_str,
            "timezone": "GMT",
        },
        timeout=10,
    )
    # Open-Meteo returns a well-formed {"error": true, "reason": "..."}
    # JSON body for invalid requests (e.g. a date outside the currently
    # available range) but, confirmed by testing, does NOT always
    # use HTTP 200 for it; a date far enough out of range comes back as
    # HTTP 400. Check the JSON body's own error field first, before
    # raise_for_status(), so an out-of-range date raises this module's
    # clean ValueError instead of a raw requests.HTTPError.
    data = response.json()

    if data.get("error"):
        raise ValueError(f"Open-Meteo API error for {start_str}..{end_str}: {data.get('reason', 'unknown error')}")

    response.raise_for_status()

    hourly = data["hourly"]
    df = pd.DataFrame({"time": pd.to_datetime(hourly["time"])})
    for raw_name, our_name in VARIABLES.items():
        df[our_name] = hourly[raw_name]

    return df
