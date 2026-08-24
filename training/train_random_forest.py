"""
Trains a single Random Forest across all panels (minus the 2 excluded
AzurSpace research cells). Generalizes to any panel via its rated
specs as input features, unlike ARIMA. This is one of the two models
(with LSTM) that can actually power the deployed app, since both
consume weather + panel specs rather than just a panel's own history,
and can react to a real Open-Meteo forecast for a requested date.
"""

import os
import sys
import time

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.dataset import load_clean_data, chronological_split
from common.features import FEATURES, TARGET, add_time_features
from evaluation.metrics import evaluate

models_dir = os.path.join(os.path.dirname(__file__), "..", "models", "random_forest")
results_path = os.path.join(os.path.dirname(__file__), "..", "output", "tables", "random_forest_results.csv")
os.makedirs(models_dir, exist_ok=True)
os.makedirs(os.path.dirname(results_path), exist_ok=True)

print("Loading and cleaning data...")
t0 = time.time()
df = load_clean_data()
df = add_time_features(df)
print(f"Loaded {len(df):,} rows in {time.time() - t0:.1f}s")

print("\nSplitting chronologically (train: 2015-2024, test: 2025)...")
train_df, test_df = chronological_split(df, test_start="2025-01-01")
print(f"Train: {len(train_df):,} rows, Test: {len(test_df):,} rows")

X_train = train_df[FEATURES]
y_train = train_df[TARGET]
X_test = test_df[FEATURES]
y_test = test_df[TARGET]

print(f"\nFeatures ({len(FEATURES)}): {FEATURES}")

model = RandomForestRegressor(
    n_estimators=200,
    max_depth=20,
    max_samples=0.3,  # each tree sees a random 30% subsample (~6.7M rows)
                       # cuts memory/time substantially on 22M+ rows,
                       # standard practice at this scale, minimal accuracy
                       # cost given how redundant the observations are
    n_jobs=-1,
    random_state=42,
    verbose=3,
)

print("\nFitting Random Forest (progress prints per tree below)...")
t0 = time.time()
model.fit(X_train, y_train)
print(f"\nFit complete in {time.time() - t0:.1f}s")

print("\nPredicting on test set...")
predictions = model.predict(X_test)

metrics = evaluate(y_test.values, predictions)
print(
    f"\nMAE={metrics['MAE']:.3f} W  RMSE={metrics['RMSE']:.3f} W  "
    f"MAPE={metrics['MAPE']:.2f}%  R2={metrics['R2']:.4f}"
)

print("\nFeature importances:")
for feat, imp in sorted(zip(FEATURES, model.feature_importances_), key=lambda x: -x[1]):
    print(f"  {feat:<25} {imp:.4f}")

model_path = os.path.join(models_dir, "random_forest.pkl")
joblib.dump(model, model_path)
print(f"\nSaved: {model_path}")

results_df = pd.DataFrame([metrics])
results_df.to_csv(results_path, index=False)
print(f"Saved: {results_path}")
