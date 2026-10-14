"""Synthetic next-hour congestion classification — demonstration only.

Labels are generated from the following hour's speed and sample density.
They are synthetic proxy labels, not true measured traffic congestion.
"""
from __future__ import annotations

import json
from datetime import timedelta

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, precision_recall_fscore_support

MODEL_VERSION = "synthetic-next-hour-v1"
FEATURE_NAMES = ("zone_id", "local_hour", "local_weekday", "gps_count", "camera_count",
                 "avg_speed", "slow_fraction")


def _features(row):
    return [float(row[k]) for k in FEATURE_NAMES]


def build_samples(connection):
    with connection.cursor() as cur:
        cur.execute("""
            SELECT zone_id, event_time, local_hour, local_weekday, gps_record_count,
                   camera_event_count, avg_speed_kph, slow_fraction
            FROM v_batch_hourly_zone_activity ORDER BY event_time, zone_id
        """)
        records = cur.fetchall()
    names = ("zone_id", "event_time", "local_hour", "local_weekday", "gps_count",
             "camera_count", "avg_speed", "slow_fraction")
    events = [dict(zip(names, x)) for x in records]
    lookup = {(x["zone_id"], x["event_time"]): x for x in events}
    samples = []
    for r in events:
        target_time = r["event_time"] + timedelta(hours=1)
        target = lookup.get((r["zone_id"], target_time))
        if target is None:
            continue
        # Unobserved future means no label. Proxy combines slower speeds and
        # elevated event density relative to this zone's expected hourly activity.
        # Do not use these target values as model input!
        congestion = target["avg_speed"] < 43.0 and target["gps_count"] >= 5
        samples.append((r, target_time, bool(congestion)))
    return samples, events


def train_and_score(connection):
    samples, events = build_samples(connection)
    hours = sorted({r["event_time"] for r, _, _ in samples})
    if len(hours) < 36:
        print("ML skipped: insufficient hourly synthetic history", flush=True)
        return
    train_cut = hours[int(len(hours) * 0.65)]
    validation_cut = hours[int(len(hours) * 0.82)]
    train = [s for s in samples if s[0]["event_time"] < train_cut]
    validation = [s for s in samples if train_cut <= s[0]["event_time"] < validation_cut]
    test = [s for s in samples if s[0]["event_time"] >= validation_cut]
    if min(len(train), len(validation), len(test)) < 10:
        print("ML skipped: insufficient time-split rows", flush=True)
        return
    x_train = np.asarray([_features(s[0]) for s in train])
    y_train = np.asarray([s[2] for s in train], dtype=int)
    model = (
        RandomForestClassifier(n_estimators=70, max_depth=6, min_samples_leaf=5,
                               random_state=20261014, n_jobs=1)
        if len(set(y_train)) > 1 else DummyClassifier(strategy="most_frequent")
    )
    model.fit(x_train, y_train)

    def positive_probability(X):
        p = model.predict_proba(np.asarray(X))
        classes = list(model.classes_)
        return p[:, classes.index(1)] if 1 in classes else np.zeros(len(X))

    def stats(rows):
        truth = np.asarray([int(s[2]) for s in rows])
        probs = positive_probability([_features(s[0]) for s in rows])
        pred = (probs >= 0.5).astype(int)
        precision, recall, f1, _ = precision_recall_fscore_support(
            truth, pred, average="binary", zero_division=0)
        return {"rows": len(rows), "positive_rate": round(float(truth.mean()), 4),
                "accuracy": round(float(accuracy_score(truth, pred)), 4),
                "balanced_accuracy": round(float(balanced_accuracy_score(truth, pred)), 4),
                "precision": round(float(precision), 4), "recall": round(float(recall), 4),
                "f1": round(float(f1), 4),
                "confusion_matrix": confusion_matrix(truth, pred, labels=[0, 1]).tolist()}

    diagnostics = {"train_size": len(train), "validation": stats(validation),
                   "test": stats(test), "label_definition": "next-hour average speed <43 km/h and >=5 GPS samples",
                   "warning": "Synthetic-only evaluation; not validated on actual traffic."}
    historical_probs = positive_probability([_features(s[0]) for s in test])
    forecasts = []
    for zone_id in range(1, 6):
        rows = [r for r in events if r["zone_id"] == zone_id]
        if not rows:
            continue
        latest = rows[-1]
        forecasts.append(latest)
    forecast_probs = positive_probability([_features(r) for r in forecasts]) if forecasts else []

    with connection.cursor() as cur:
        cur.execute("DELETE FROM ml_congestion_predictions WHERE model_version=%s", (MODEL_VERSION,))
        for (r, target_hour, actual), probability in zip(test, historical_probs):
            _save(cur, "holdout", r, target_hour, probability, actual)
        for r, probability in zip(forecasts, forecast_probs):
            _save(cur, "forecast", r, r["event_time"] + timedelta(hours=1), probability, None)
        cur.execute("""
            INSERT INTO ml_model_metadata(model_version, metrics_json)
            VALUES (%s,%s)
            ON CONFLICT(model_version) DO UPDATE
            SET metrics_json=excluded.metrics_json, trained_at=now()
        """, (MODEL_VERSION, json.dumps(diagnostics)))
    connection.commit()
    print("Synthetic next-hour ML diagnostics: " + json.dumps(diagnostics), flush=True)


def _save(cur, kind, r, target_hour, probability, actual):
    cur.execute("""
      INSERT INTO ml_congestion_predictions
      (forecast_kind,zone_id,feature_hour,target_hour,congestion_probability,
       predicted_congestion,actual_congestion,gps_record_count,camera_event_count,
       avg_speed_kph,model_version)
      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
      ON CONFLICT(forecast_kind,zone_id,feature_hour) DO UPDATE
      SET congestion_probability=excluded.congestion_probability,
          predicted_congestion=excluded.predicted_congestion,
          actual_congestion=excluded.actual_congestion,
          model_version=excluded.model_version
    """, (kind, r["zone_id"], r["event_time"], target_hour, float(probability),
          bool(probability >= 0.5), actual, r["gps_count"], r["camera_count"],
          r["avg_speed"], MODEL_VERSION))
