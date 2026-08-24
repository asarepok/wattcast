"""
Canonical feature list, shared by every model that trains on the full
235-panel dataset (Random Forest, LSTM). Keeping this in one place
means both see identical inputs, which matters for a fair comparison.
"""

WEATHER_FEATURES = [
    "temperature_2m_c",
    "shortwave_radiation_wm2",
    "relative_humidity_pct",
    "cloud_cover_pct",
    "wind_speed_10m_kmh",
    "precipitation_mm",
    "dew_point_2m_c",
]

PANEL_FEATURES = [
    "rated_pmax_w",
    "rated_voc_v",
    "rated_isc_a",
    "rated_vmp_v",
    "rated_imp_a",
    "n_series",
    "n_parallel",
]

# Plain integers, not cyclically (sin/cos) encoded: tree-based models
# don't need that trick (a tree can just split on "hour <= 5 OR hour >=
# 22" directly), unlike models that assume smooth/linear relationships.
TIME_FEATURES = [
    "month",
    "hour",
]

FEATURES = WEATHER_FEATURES + PANEL_FEATURES + TIME_FEATURES

TARGET = "output_power_w"

# output_voltage_v and output_current_a are NEVER features. Power is
# derived from them (P = V * I), so including them would let a model
# trivially reconstruct the target instead of learning the real
# weather -> power relationship.


def add_time_features(df):
    df = df.copy()
    df["month"] = df["time"].dt.month
    df["hour"] = df["time"].dt.hour
    return df
