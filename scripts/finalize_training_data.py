"""
Finalizes a freshly generated Simscape simulation run:
  - converts data/simulated/training_data_all_panels.csv to
    data/processed/training_data.parquet, streamed via DuckDB so the
    ~5.8 GB CSV never gets loaded into RAM at once.
  - extracts the 3 representative panels used for the ARIMA comparison
    (small/medium/large rated capacity) into
    data/processed/arima_panels.parquet.
"""

import os
import duckdb

base_dir = os.path.dirname(__file__)
csv_path = os.path.join(base_dir, "..", "data", "simulated", "training_data_all_panels.csv")
parquet_path = os.path.join(base_dir, "..", "data", "processed", "training_data.parquet")
arima_path = os.path.join(base_dir, "..", "data", "processed", "arima_panels.parquet")

con = duckdb.connect()
con.execute("SET memory_limit='8GB'")

COLUMNS = {
    "time": "VARCHAR",
    "temperature_2m_c": "DOUBLE",
    "shortwave_radiation_wm2": "DOUBLE",
    "relative_humidity_pct": "DOUBLE",
    "cloud_cover_pct": "DOUBLE",
    "wind_speed_10m_kmh": "DOUBLE",
    "precipitation_mm": "DOUBLE",
    "dew_point_2m_c": "DOUBLE",
    "manufacturer": "VARCHAR",
    "model_name": "VARCHAR",
    "part_id": "VARCHAR",
    "rated_pmax_w": "DOUBLE",
    "rated_voc_v": "DOUBLE",
    "rated_isc_a": "DOUBLE",
    "rated_vmp_v": "DOUBLE",
    "rated_imp_a": "DOUBLE",
    "n_series": "DOUBLE",
    "n_parallel": "DOUBLE",
    "output_voltage_v": "DOUBLE",
    "output_current_a": "DOUBLE",
    "output_power_w": "DOUBLE",
}
columns_literal = ", ".join(f"'{k}': '{v}'" for k, v in COLUMNS.items())

print("Converting simulated CSV -> Parquet ...")
con.execute(f"""
    COPY (
        SELECT
            strptime(time, '%d-%b-%Y %H:%M:%S') AS time,
            * EXCLUDE (time)
        FROM read_csv(
            '{csv_path}',
            columns = {{{columns_literal}}},
            header = true
        )
    ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
""")

n_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')").fetchone()[0]
print(f"Wrote: {parquet_path} ({n_rows:,} rows)")

ARIMA_PANELS = [
    "FirstSolar|FS_390_PLUS|simscape.electrical.solarCell",
    "Amerisolar|AS_6M30_HC_320W|simscape.electrical.solarCell",
    "Telesun|TP7F78M_H_590|simscape.electrical.solarCell",
]
part_ids_literal = ", ".join(f"'{p}'" for p in ARIMA_PANELS)

print("Extracting ARIMA representative panels ...")
con.execute(f"""
    COPY (
        SELECT *
        FROM read_parquet('{parquet_path}')
        WHERE part_id IN ({part_ids_literal})
        ORDER BY part_id, time
    ) TO '{arima_path}' (FORMAT PARQUET, COMPRESSION ZSTD);
""")

n_arima_rows = con.execute(f"SELECT COUNT(*) FROM read_parquet('{arima_path}')").fetchone()[0]
print(f"Wrote: {arima_path} ({n_arima_rows:,} rows)")
