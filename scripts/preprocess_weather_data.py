"""
Preprocesses the raw Open-Meteo export (data/raw/weather_data.csv) into
a clean hourly weather table, before it's used as simulation input:
  - skip the site-metadata header Open-Meteo prepends (lat/long/
    elevation/timezone) and parse the actual hourly data table.
  - rename columns to plain snake_case (matching the naming already
    used downstream in training_data.parquet).
  - parse timestamps and check for: missing values, duplicate
    timestamps, gaps in the hourly sequence, and out-of-range values.
"""

import os
import pandas as pd

base_dir = os.path.dirname(__file__)
in_path = os.path.join(base_dir, "..", "data", "raw", "weather_data.csv")
out_path = os.path.join(base_dir, "..", "data", "processed", "weather_data.csv")

site_meta = pd.read_csv(in_path, nrows=1)
print("Site metadata:")
print(site_meta.to_string(index=False))
print()

df = pd.read_csv(in_path, skiprows=3)

df = df.rename(columns={
    "time": "time",
    "temperature_2m (°C)": "temperature_2m_c",
    "shortwave_radiation (W/m²)": "shortwave_radiation_wm2",
    "relative_humidity_2m (%)": "relative_humidity_pct",
    "cloud_cover (%)": "cloud_cover_pct",
    "wind_speed_10m (km/h)": "wind_speed_10m_kmh",
    "precipitation (mm)": "precipitation_mm",
    "dew_point_2m (°C)": "dew_point_2m_c",
})

df["time"] = pd.to_datetime(df["time"], format="%Y-%m-%dT%H:%M")
df = df.sort_values("time").reset_index(drop=True)

print(f"Rows: {len(df):,}")
print(f"Span: {df['time'].min()} -> {df['time'].max()}")
print()

# missing values
n_missing = df.isna().sum()
print("Missing values per column:")
print(n_missing.to_string())
print()

# duplicate timestamps
n_dupes = df["time"].duplicated().sum()
print(f"Duplicate timestamps: {n_dupes}")

# gaps in the hourly sequence
expected_range = pd.date_range(df["time"].min(), df["time"].max(), freq="h")
missing_hours = expected_range.difference(df["time"])
print(f"Missing hours in sequence: {len(missing_hours)}")
if len(missing_hours) > 0:
    print(missing_hours[:10].to_list(), "..." if len(missing_hours) > 10 else "")
print()

# range sanity checks
range_checks = {
    "temperature_2m_c": (-10, 55),
    "shortwave_radiation_wm2": (0, 1500),
    "relative_humidity_pct": (0, 100),
    "cloud_cover_pct": (0, 100),
    "wind_speed_10m_kmh": (0, 200),
    "precipitation_mm": (0, 300),
    "dew_point_2m_c": (-20, 40),
}
print("Out-of-range values:")
any_out_of_range = False
for col, (lo, hi) in range_checks.items():
    n_out = ((df[col] < lo) | (df[col] > hi)).sum()
    if n_out > 0:
        any_out_of_range = True
    print(f"  {col}: {n_out} outside [{lo}, {hi}]")
if not any_out_of_range:
    print("  none")
print()

df.to_csv(out_path, index=False)
print(f"Wrote: {out_path}")
