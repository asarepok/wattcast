
"""
Trains an LSTM across all panels (minus the 2 excluded AzurSpace
research cells), using a 24-hour weather window plus static panel specs
to predict each hour's power. This is the second of the two models
(with Random Forest) that can actually power the deployed app, since
both consume weather + panel specs and can react to a real forecast for
a requested date.
"""

import os
import sys
import time

import joblib
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.preprocessing import MinMaxScaler
from torch.utils.data import DataLoader

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.dataset import WindowedSolarDataset, chronological_split, load_clean_data
from common.features import PANEL_FEATURES, TARGET, WEATHER_FEATURES
from evaluation.metrics import evaluate

models_dir = os.path.join(os.path.dirname(__file__), "..", "models", "lstm")
results_path = os.path.join(os.path.dirname(__file__), "..", "output", "tables", "lstm_results.csv")
os.makedirs(models_dir, exist_ok=True)
os.makedirs(os.path.dirname(results_path), exist_ok=True)

WINDOW_HOURS = 24
HIDDEN_SIZE = 32
BATCH_SIZE = 256
N_EPOCHS = 15
LEARNING_RATE = 0.001
# 20M+ possible training windows would make every epoch impractically
# slow to iterate. A random subsample still gives the model plenty of
# diverse examples (same reasoning as Random Forest's max_samples=0.3).
MAX_TRAIN_WINDOWS = 2_000_000
PRINT_EVERY = 200  # batches

device = (
    "cuda" if torch.cuda.is_available()
    else "mps" if torch.backends.mps.is_available()
    else "cpu"
)
print(f"Using device: {device}")

print("\nLoading and cleaning data...")
t0 = time.time()
df = load_clean_data()
print(f"Loaded {len(df):,} rows in {time.time() - t0:.1f}s")

print("\nSplitting chronologically (train: 2015-2024, test: 2025)...")
train_df, test_df = chronological_split(df, test_start="2025-01-01")
print(f"Train: {len(train_df):,} rows, Test: {len(test_df):,} rows")
del df

print("\nFitting scalers on training data only...")
weather_scaler = MinMaxScaler()
weather_scaler.fit(train_df[WEATHER_FEATURES])

panel_scaler = MinMaxScaler()
panel_scaler.fit(train_df[PANEL_FEATURES])

target_scaler = MinMaxScaler()
target_scaler.fit(train_df[[TARGET]])

# sklearn's transform() returns float64. Cast straight back to
# float32 or this silently doubles memory right at the point we've
# been most careful about it all project.
for d in (train_df, test_df):
    d[WEATHER_FEATURES] = weather_scaler.transform(d[WEATHER_FEATURES]).astype("float32")
    d[PANEL_FEATURES] = panel_scaler.transform(d[PANEL_FEATURES]).astype("float32")
    d[TARGET] = target_scaler.transform(d[[TARGET]]).astype("float32")

print("\nBuilding windowed datasets (this constructs each window on demand, not upfront)...")
t0 = time.time()
train_dataset = WindowedSolarDataset(train_df, window_hours=WINDOW_HOURS, max_samples=MAX_TRAIN_WINDOWS)
test_dataset = WindowedSolarDataset(test_df, window_hours=WINDOW_HOURS)
print(f"Train windows: {len(train_dataset):,}, Test windows: {len(test_dataset):,} ({time.time() - t0:.1f}s)")
del train_df, test_df

train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, shuffle=True, num_workers=0)
test_loader = DataLoader(test_dataset, batch_size=1024, shuffle=False, num_workers=0)


class LSTMModel(nn.Module):
    def __init__(self, n_weather, n_static, hidden_size=32):
        super().__init__()
        self.lstm = nn.LSTM(input_size=n_weather, hidden_size=hidden_size, batch_first=True)
        self.fc1 = nn.Linear(hidden_size + n_static, 16)
        self.relu = nn.ReLU()
        self.fc2 = nn.Linear(16, 1)

    def forward(self, weather_seq, static):
        out, _ = self.lstm(weather_seq)
        last_step = out[:, -1, :]
        combined = torch.cat([last_step, static], dim=1)
        return self.fc2(self.relu(self.fc1(combined)))


model = LSTMModel(len(WEATHER_FEATURES), len(PANEL_FEATURES), HIDDEN_SIZE).to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)
loss_fn = nn.MSELoss()

print(f"\n{model}")
print(f"Total parameters: {sum(p.numel() for p in model.parameters()):,}")

model_path = os.path.join(models_dir, "lstm.pth")

print(f"\nTraining for {N_EPOCHS} epochs, {len(train_loader):,} batches/epoch...")
for epoch in range(N_EPOCHS):
    model.train()
    running_loss = 0.0
    t0 = time.time()

    for batch_idx, (weather_seq, static, target) in enumerate(train_loader):
        weather_seq = weather_seq.to(device)
        static = static.to(device)
        target = target.to(device)

        optimizer.zero_grad()
        pred = model(weather_seq, static)
        loss = loss_fn(pred, target)
        loss.backward()
        optimizer.step()

        running_loss += loss.item()

        if batch_idx % PRINT_EVERY == 0:
            print(f"  Epoch {epoch + 1}/{N_EPOCHS}  Batch {batch_idx:5d}/{len(train_loader)}  Loss: {loss.item():.6f}")

    avg_loss = running_loss / len(train_loader)
    print(f"Epoch {epoch + 1}/{N_EPOCHS} complete, avg loss: {avg_loss:.6f}  ({time.time() - t0:.1f}s)")

    # Checkpoint after every epoch, not just at the end. Crash
    # resilience given how many long runs have died mid-way this project.
    torch.save(model.state_dict(), model_path)

print("\nEvaluating on test set...")
model.eval()
all_preds = []
all_targets = []
with torch.no_grad():
    for weather_seq, static, target in test_loader:
        weather_seq = weather_seq.to(device)
        static = static.to(device)
        pred = model(weather_seq, static).cpu().numpy()
        all_preds.append(pred)
        all_targets.append(target.numpy())

all_preds = np.concatenate(all_preds)
all_targets = np.concatenate(all_targets)

# Undo scaling before computing metrics: want MAE/RMSE in real watts,
# not 0-1 scaled units.
all_preds_w = target_scaler.inverse_transform(all_preds).clip(min=0)
all_targets_w = target_scaler.inverse_transform(all_targets)

metrics = evaluate(all_targets_w.ravel(), all_preds_w.ravel())
print(
    f"\nMAE={metrics['MAE']:.3f} W  RMSE={metrics['RMSE']:.3f} W  "
    f"MAPE={metrics['MAPE']:.2f}%  R2={metrics['R2']:.4f}"
)

torch.save(model.state_dict(), model_path)
print(f"\nSaved: {model_path}")

scalers_path = os.path.join(models_dir, "scalers.pkl")
joblib.dump({"weather": weather_scaler, "panel": panel_scaler, "target": target_scaler}, scalers_path)
print(f"Saved: {scalers_path}")

results_df = pd.DataFrame([metrics])
results_df.to_csv(results_path, index=False)
print(f"Saved: {results_path}")
