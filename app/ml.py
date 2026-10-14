"""Train, evaluate and persist synthetic congestion/speed models.

Both models use the shared trailing-30-minute feature SQL in app.features.
The next-hour targets are proxy labels derived strictly after each observation.
"""
from __future__ import annotations

import json
import os
import pickle
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.metrics import (accuracy_score, balanced_accuracy_score, confusion_matrix,
                             f1_score, mean_absolute_error, mean_squared_error,
                             precision_score, r2_score, recall_score)

from app.features import FEATURE_NAMES, FEATURE_SQL, feature_values

MODEL_VERSION = "live-window-rf-v4"
MODEL_DIR = Path(os.getenv("MODEL_DIR", "/models"))


def _fetch_features(cur, anchor, mode):
    args = (anchor, anchor, anchor, anchor, anchor, anchor, anchor,
            mode, anchor, anchor, anchor, mode, anchor, anchor, mode, anchor, anchor)
    cur.execute(FEATURE_SQL, args)
    names = [d.name for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def build_training_samples(connection):
    """Produce per-zone half-hourly examples; inputs precede targets by construction."""
    with connection.cursor() as cur:
        cur.execute("""SELECT date_trunc('minute', min(event_time)) + interval '30 minutes',
                            date_trunc('minute', max(event_time)) - interval '60 minutes'
                     FROM vehicle_positions WHERE ingestion_mode='batch'""")
        start, stop = cur.fetchone()
        if not start or not stop or stop <= start:
            return []
        cur.execute("SELECT generate_series(%s::timestamptz,%s::timestamptz,interval '30 minutes')", (start, stop))
        anchors = [r[0] for r in cur.fetchall()]
        examples = []
        for anchor in anchors:
            for row in _fetch_features(cur, anchor, "batch"):
                if row["gps_count"] < 1:
                    continue
                cur.execute("""SELECT count(*)::int, avg(speed_kph)::double precision
                               FROM vehicle_positions WHERE zone_id=%s AND ingestion_mode='batch'
                                 AND event_time > %s AND event_time <= %s""",
                            (row["zone_id"], anchor, anchor + timedelta(hours=1)))
                coverage, target_speed = cur.fetchone()
                if not target_speed or coverage < 5:
                    continue
                examples.append({"anchor": anchor, "row": row, "x": feature_values(row),
                                 "y_class": int(target_speed < 43.0), "y_speed": float(target_speed)})
    return examples


def _metrics(y, pred, labels=(0, 1)):
    return {"accuracy": float(accuracy_score(y, pred)),
            "balanced_accuracy": float(balanced_accuracy_score(y, pred)),
            "precision": float(precision_score(y, pred, zero_division=0)),
            "recall": float(recall_score(y, pred, zero_division=0)),
            "f1": float(f1_score(y, pred, zero_division=0)),
            "confusion_matrix": confusion_matrix(y, pred, labels=list(labels)).tolist()}


def train_and_score(connection, force=True):
    examples = build_training_samples(connection)
    if len(examples) < 100:
        raise RuntimeError(f"Insufficient historical feature/target samples: {len(examples)}")
    anchors = sorted({e["anchor"] for e in examples})
    train_end = anchors[int(len(anchors) * .65)]
    valid_end = anchors[int(len(anchors) * .82)]
    # 90-minute purge ensures previous split labels do not enter later split features/targets.
    train = [e for e in examples if e["anchor"] + timedelta(hours=1) < train_end]
    valid = [e for e in examples if e["anchor"] >= train_end + timedelta(minutes=90)
             and e["anchor"] + timedelta(hours=1) < valid_end]
    test = [e for e in examples if e["anchor"] >= valid_end + timedelta(minutes=90)]
    if min(map(len, (train, valid, test))) < 20:
        raise RuntimeError(f"Insufficient purged chronological split: {len(train)}/{len(valid)}/{len(test)}")
    Xtr = np.asarray([e["x"] for e in train]); Xv = np.asarray([e["x"] for e in valid]); Xt = np.asarray([e["x"] for e in test])
    ytr = np.asarray([e["y_class"] for e in train]); yv = np.asarray([e["y_class"] for e in valid]); yt = np.asarray([e["y_class"] for e in test])
    yrtr = np.asarray([e["y_speed"] for e in train]); yrv = np.asarray([e["y_speed"] for e in valid]); yrt = np.asarray([e["y_speed"] for e in test])
    if len(np.unique(ytr)) < 2 or len(np.unique(yv)) < 2 or len(np.unique(yt)) < 2:
        raise RuntimeError("Both classes are required in training, validation and test partitions")

    classifier = RandomForestClassifier(n_estimators=120, max_depth=8, min_samples_leaf=4,
                                        class_weight="balanced_subsample", random_state=20261014, n_jobs=1)
    regressor = RandomForestRegressor(n_estimators=120, max_depth=8, min_samples_leaf=4,
                                      random_state=20261014, n_jobs=1)
    classifier.fit(Xtr, ytr); regressor.fit(Xtr, yrtr)
    classes = list(classifier.classes_)
    vprob = classifier.predict_proba(Xv)[:, classes.index(1)]
    thresholds = sorted({0.1, 0.9, *(float(p) for p in vprob)})
    threshold = max(thresholds, key=lambda t: (f1_score(yv, vprob >= t, zero_division=0), t))
    class_pred = (classifier.predict_proba(Xt)[:, classes.index(1)] >= threshold).astype(int)
    majority_value = int(np.bincount(ytr).argmax()); majority_pred = np.full(len(yt), majority_value)
    cls_metrics = _metrics(yt, class_pred)
    cls_baseline = _metrics(yt, majority_pred)
    pred_speed = regressor.predict(Xt)
    persistence = np.asarray([e["row"]["avg_speed_kph"] or 0 for e in test], dtype=float)
    reg_metrics = {"mae": float(mean_absolute_error(yrt, pred_speed)),
                   "rmse": float(mean_squared_error(yrt, pred_speed) ** .5),
                   "r2": float(r2_score(yrt, pred_speed)), "test_size": len(test)}
    reg_baseline = {"mae": float(mean_absolute_error(yrt, persistence)),
                    "rmse": float(mean_squared_error(yrt, persistence) ** .5),
                    "r2": float(r2_score(yrt, persistence))}
    trained_at = __import__("datetime").datetime.now(timezone.utc)
    metadata = {"model_version": MODEL_VERSION, "model_type": "RandomForestClassifier + RandomForestRegressor",
                "trained_at": trained_at.isoformat(), "feature_names": list(FEATURE_NAMES),
                "feature_window_minutes": 30, "prediction_horizon_minutes": 60,
                "label_definition": "next-hour mean speed < 43 km/h, with at least 5 GPS observations",
                "split": {"train_size": len(train), "validation_size": len(valid), "test_size": len(test),
                          "train_end_exclusive": train_end.isoformat(), "validation_end_exclusive": valid_end.isoformat(),
                          "purge_minutes": 90},
                "classification": {"threshold": float(threshold), "positive_class_rate": float(yt.mean()),
                                   "positive_support": int(yt.sum()), "test": cls_metrics,
                                   "majority_baseline": cls_baseline},
                "regression": {"test": reg_metrics, "persistence_baseline": reg_baseline},
                "synthetic_warning": "Synthetic-only proxy evaluation; not validated on real traffic."}
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    artifact = {"classifier": classifier, "regressor": regressor, "metadata": metadata,
                "feature_names": FEATURE_NAMES, "version": MODEL_VERSION}
    artifact_path = MODEL_DIR / f"{MODEL_VERSION}.pkl"
    with artifact_path.open("wb") as f:
        pickle.dump(artifact, f, protocol=pickle.HIGHEST_PROTOCOL)

    with connection.cursor() as cur:
        cur.execute("DELETE FROM ml_congestion_predictions WHERE forecast_kind='holdout'")
        cur.execute("DELETE FROM ml_regression_holdout")
        for e, cp, sp in zip(test, classifier.predict_proba(Xt)[:, classes.index(1)], pred_speed):
            row = e["row"]
            cur.execute("""INSERT INTO ml_congestion_predictions
              (forecast_kind,zone_id,feature_hour,target_hour,congestion_probability,predicted_congestion,
               actual_congestion,gps_record_count,camera_event_count,avg_speed_kph,model_version)
              VALUES ('holdout',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(forecast_kind,zone_id,feature_hour) DO UPDATE SET
                target_hour=excluded.target_hour,congestion_probability=excluded.congestion_probability,
                predicted_congestion=excluded.predicted_congestion,actual_congestion=excluded.actual_congestion,
                gps_record_count=excluded.gps_record_count,camera_event_count=excluded.camera_event_count,
                avg_speed_kph=excluded.avg_speed_kph,model_version=excluded.model_version""",
                        (row["zone_id"], e["anchor"], e["anchor"]+timedelta(hours=1), float(cp),
                         bool(cp >= threshold), bool(e["y_class"]), row["gps_count"], row["camera_count"],
                         row["avg_speed_kph"] or 0, MODEL_VERSION))
            cur.execute("""INSERT INTO ml_regression_holdout
              (zone_id,observation_time,target_start,target_end,actual_avg_speed_kph,predicted_avg_speed_kph,
               persistence_avg_speed_kph,model_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (row["zone_id"],e["anchor"],e["anchor"],e["anchor"]+timedelta(hours=1),
                         e["y_speed"],float(sp),float(row["avg_speed_kph"] or 0),MODEL_VERSION))
        cur.execute("""INSERT INTO ml_model_metadata(model_version,metrics_json)
                       VALUES (%s,%s) ON CONFLICT(model_version) DO UPDATE
                       SET metrics_json=excluded.metrics_json,trained_at=now()""", (MODEL_VERSION,json.dumps(metadata)))
    connection.commit()
    print(f"ML training complete: artifact={artifact_path}; metadata={json.dumps(metadata)}", flush=True)
    return artifact


def load_models():
    path = MODEL_DIR / f"{MODEL_VERSION}.pkl"
    with path.open("rb") as f:
        artifact = pickle.load(f)  # Local named volume is trusted application-owned state.
    if tuple(artifact.get("feature_names", ())) != FEATURE_NAMES or artifact.get("version") != MODEL_VERSION:
        raise RuntimeError("Persisted model feature schema/version mismatch")
    return artifact


def infer_live(connection, artifact=None):
    """Persist one idempotent live inference cycle. Insufficient zones get explicit status rows."""
    from datetime import datetime, timezone
    from app.features import FEATURE_NAMES, feature_values
    import numpy as np
    artifact = artifact or load_models()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=30); target_end = now + timedelta(hours=1)
    metadata = artifact["metadata"]
    clf, reg = artifact["classifier"], artifact["regressor"]
    threshold = metadata["classification"]["threshold"]
    results = []
    with connection.cursor() as cur:
        cur.execute("""INSERT INTO ml_inference_runs(prediction_time,feature_window_start,target_end,status)
                     VALUES (%s,%s,%s,'running') ON CONFLICT(prediction_time) DO NOTHING RETURNING prediction_time""",
                    (now,start,target_end))
        if cur.fetchone() is None:
            return 0
        rows = _fetch_features(cur, now, "stream")
        for row in rows:
            cur.execute("SAVEPOINT zone_prediction")
            try:
                enough = (row["gps_count"] >= 20 and row["vehicle_count"] >= 3
                      and row["avg_speed_kph"] is not None and row["window_head_event_time"] is not None)
                status = "ok" if enough else "insufficient_data"
                probability = predicted = speed = current = change = None
                if enough:
                    X = np.asarray([feature_values(row)])
                    probabilities = clf.predict_proba(X)[0]; classes = list(clf.classes_)
                    probability = float(probabilities[classes.index(1)]) if 1 in classes else 0.0
                    predicted = probability >= threshold
                    current = float(row["avg_speed_kph"])
                    speed = max(0.0, float(reg.predict(X)[0]))
                    change = (speed-current)/current*100 if current else None
                error = None
            except Exception as exc:
                status, probability, predicted, speed, current, change = "error", None, None, None, None, None
                error = f"{type(exc).__name__}: {exc}"[:1000]
            cur.execute("""INSERT INTO ml_live_predictions(zone_id,prediction_time,feature_window_start,
              feature_window_end,target_start,target_end,congestion_probability,predicted_congested,
              current_avg_speed_kph,predicted_avg_speed_kph,predicted_speed_change_pct,
              classifier_version,regressor_version,prediction_status,error_message,gps_record_count,camera_event_count)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(zone_id,prediction_time) DO NOTHING""",
              (row["zone_id"],now,start,now,now,target_end,probability,predicted,current,speed,change,
               artifact["version"],artifact["version"],status,error,row["gps_count"],row["camera_count"]))
            cur.execute("RELEASE SAVEPOINT zone_prediction")
            results.append(status)
        success = results.count("ok")
        cur.execute("""UPDATE ml_inference_runs SET status=%s,prediction_count=%s,
                     successful_zone_count=%s,insufficient_zone_count=%s,failed_zone_count=%s,completed_at=now()
                     WHERE prediction_time=%s""",
                    ("ok" if success == 5 else "partial" if success else "insufficient_data",
                     len(results),success,results.count("insufficient_data"),results.count("error"),now))
    connection.commit()
    print(f"Live ML inference {now.isoformat()}: successful_zones={results.count('ok')}/5 statuses={results}",flush=True)
    return 1


def record_inference_error(connection, exc):
    """Persist a failed cycle after the caller rolls back its failed transaction."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    try:
        with connection.cursor() as cur:
            cur.execute("""INSERT INTO ml_inference_runs(prediction_time,feature_window_start,target_end,status,
                         error_message,completed_at) VALUES (%s,%s,%s,'error',%s,now())
                         ON CONFLICT(prediction_time) DO UPDATE SET status='error',error_message=excluded.error_message,
                         completed_at=now()""",
                        (now,now-timedelta(minutes=30),now+timedelta(hours=1),f"{type(exc).__name__}: {exc}"[:2000]))
        connection.commit()
    except Exception:
        connection.rollback()
