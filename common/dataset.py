"""
Shared data loading and chronological train/test splitting, used by
every model that trains on the full 235-panel dataset (Random Forest,
LSTM). Keeping this in one place means every model sees the exact same
cleaning, exclusions, and split boundary, required for a fair
comparison between them.
"""

import os

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import torch
from torch.utils.data import Dataset

from common.features import PANEL_FEATURES, TARGET, WEATHER_FEATURES

DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "training_data.parquet")

# Bare single-cell space-grade research parts (n_series=1, n_parallel=1),
# not assembled modules like every other part in the collection. Their
# near-zero simulated output is a load/circuit-mismatch artifact, not
# real physics, so it's excluded to keep the model from learning "these panels
# always produce ~nothing" as if it were true capability.
EXCLUDED_PART_IDS = [
    "AzurSpace|3C44_30sqmm|simscape.electrical.solarCell",
    "AzurSpace|3C44_9sqmm|simscape.electrical.solarCell",
]

CATEGORICAL_COLUMNS = ["manufacturer", "model_name", "part_id"]


def load_clean_data():
    """
    Loads training_data.parquet memory-consciously: converts
    manufacturer/model_name/part_id straight to pandas categoricals
    during the Arrow->pandas step (avoiding a spike from materializing
    22M+ individual Python string objects), clips the small negative-
    power simulation noise to physically-valid 0, excludes the 2
    AzurSpace panels, and downcasts float64 -> float32.
    """
    table = pq.read_table(DATA_PATH)
    df = table.to_pandas(categories=CATEGORICAL_COLUMNS)
    del table

    df = df[~df["part_id"].isin(EXCLUDED_PART_IDS)].reset_index(drop=True)
    # Filtering rows doesn't drop the now-empty AzurSpace categories from
    # the Categorical dtype itself. Without this, a later groupby("part_id")
    # (LSTM's windowing) would try to form empty groups for them and crash.
    for col in CATEGORICAL_COLUMNS:
        df[col] = df[col].cat.remove_unused_categories()

    df["output_power_w"] = df["output_power_w"].clip(lower=0)

    float_cols = df.select_dtypes(include="float64").columns
    df[float_cols] = df[float_cols].astype("float32")

    return df


def chronological_split(df, test_start="2025-01-01"):
    """
    Splits by time, not randomly: training only ever sees the past,
    testing only ever sees genuinely later dates, so no future
    information leaks backward. test_start defaults to holding out the
    full final year so evaluation spans every season, not just one
    slice of the year.
    """
    test_start = pd.Timestamp(test_start)
    train = df[df["time"] < test_start].reset_index(drop=True)
    test = df[df["time"] >= test_start].reset_index(drop=True)
    return train, test


class WindowedSolarDataset(Dataset):
    """
    Builds (window_hours-long weather sequence, static panel specs) ->
    power examples on the fly, for LSTM training. Deliberately windows
    weather only, not past power. RF's feature importances already
    showed weather alone gets R^2 extremely close to 1.0 with zero lag
    features, and including past power would create a chicken-and-egg
    problem at real inference time (a genuinely future date has no real
    "recent actual power" to feed in).

    Never materializes the full windowed tensor upfront: 233 panels x
    ~96k hours x 24-hour windows would be tens of GB. Instead stores
    each panel's full weather/power arrays once (~small) and slices out
    each requested window at __getitem__ time.
    """

    def __init__(self, df, window_hours=24, max_samples=None, seed=0):
        self.window_hours = window_hours

        self.panel_weather = []
        self.panel_power = []
        self.panel_static = []

        panel_index_parts = []
        time_index_parts = []

        for panel_idx, (part_id, g) in enumerate(df.groupby("part_id", sort=False, observed=True)):
            g = g.sort_values("time")

            # .copy() (not just ascontiguousarray, which only copies when
            # NOT already contiguous), since pandas' copy-on-write can hand
            # back read-only arrays from .to_numpy() that are already
            # contiguous, which ascontiguousarray then passes through
            # unchanged, still read-only, tripping torch.from_numpy's
            # writability warning at __getitem__ time.
            self.panel_weather.append(g[WEATHER_FEATURES].to_numpy(dtype="float32").copy())
            self.panel_power.append(g[TARGET].to_numpy(dtype="float32").copy())
            self.panel_static.append(g[PANEL_FEATURES].iloc[0].to_numpy(dtype="float32").copy())

            n = len(g)
            valid_t = np.arange(window_hours - 1, n, dtype="int32")
            panel_index_parts.append(np.full(valid_t.shape, panel_idx, dtype="int32"))
            time_index_parts.append(valid_t)

        self.panel_indices = np.concatenate(panel_index_parts)
        self.time_indices = np.concatenate(time_index_parts)

        if max_samples is not None and len(self.panel_indices) > max_samples:
            rng = np.random.default_rng(seed)
            keep = rng.choice(len(self.panel_indices), size=max_samples, replace=False)
            self.panel_indices = self.panel_indices[keep]
            self.time_indices = self.time_indices[keep]

    def __len__(self):
        return len(self.panel_indices)

    def __getitem__(self, i):
        panel_idx = int(self.panel_indices[i])
        t = int(self.time_indices[i])

        weather = self.panel_weather[panel_idx]
        window = weather[t - self.window_hours + 1: t + 1]
        static = self.panel_static[panel_idx]
        target = self.panel_power[panel_idx][t:t + 1]

        return (
            torch.from_numpy(window),
            torch.from_numpy(static),
            torch.from_numpy(target),
        )
