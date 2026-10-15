"""
Lab 3 anomaly / incident monitor for all Boutique services.

Generalisation of monitor.py:
1. At startup, train one Prophet model (flat trend, no seasonality) per service
   on its p95 response-time history in training_data/<service>_responsetime.json
   (produced by fetch_training_data.py).
2. Every cycle, pull a single p95 datapoint per service for the last minute.
   One instant query (no [x:y] sub-query) grouped by destination service returns
   one value for every service at once. Each value becomes a 1-record dataframe
   that is evaluated by that service's model; anomaly = value above yhat_upper.
3. Keep a dataframe of anomaly results per service; if a service's last 4
   entries are all anomalies, the service is in an incident.
4. Publish both values as one Prometheus gauge with labels service / kind, and
   print a summary dataframe of all services on each cycle.

Configuration (environment variables):
    PROMETHEUS_URL     Prometheus base URL          (default http://prometheus.istio-system:9090;
                       use http://localhost:9090 when running locally)
    SERVICES           comma-separated service list (default: every *_responsetime.json in TRAINING_DIR)
    TRAINING_DIR       directory with training JSON (default training_data next to this file)
    POLL_INTERVAL      seconds between checks        (default 60)
    METRICS_PORT       port for the /metrics server  (default 8099)
    INCIDENT_WINDOW    consecutive anomalies needed  (default 4)
"""

import glob
import json
import logging
import math
import os
import time
from datetime import datetime

import pandas as pd
import requests
from prometheus_client import Gauge, start_http_server
from prophet import Prophet

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus.istio-system:9090")
TRAINING_DIR = os.getenv(
    "TRAINING_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "training_data")
)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8099"))
INCIDENT_WINDOW = int(os.getenv("INCIDENT_WINDOW", "4"))
HISTORY_ROWS = 1440  # per-service rows kept in memory

QUERY_URL = f"{PROMETHEUS_URL}/api/v1/query"

# p95 response time over the last minute for every destination service.
# No [x:y] sub-query at the end, so Prometheus returns one value per service.
LATEST_QUERY = (
    "histogram_quantile( 0.95, sum by (le, destination_canonical_service) (increase("
    "istio_request_duration_milliseconds_bucket{reporter='source'}[1m])))"
)

# Quiet Prophet / cmdstanpy chatter so the console shows the monitor output.
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)

HISTORY_COLUMNS = ["ds", "y", "yhat", "yhat_lower", "yhat_upper", "anomaly", "incident"]

# Single metric name, labelled by service and kind ("anomaly" or "incident").
g = Gauge(
    "lab3_anomalies_and_incidents",
    "Gauge for tracking anomalies and incidents across all services",
    ["service", "kind"],
)


def list_services():
    env = os.getenv("SERVICES")
    if env:
        return [s.strip() for s in env.split(",") if s.strip()]
    files = glob.glob(os.path.join(TRAINING_DIR, "*_responsetime.json"))
    return sorted(os.path.basename(f)[: -len("_responsetime.json")] for f in files)


def load_training_data(service):
    """Return the training dataframe (ds, y) for one service."""
    with open(os.path.join(TRAINING_DIR, f"{service}_responsetime.json")) as f:
        prom = json.load(f)
    df_train = pd.DataFrame(prom["data"]["result"][0]["values"])
    df_train.columns = ["ds", "y"]
    df_train["ds"] = df_train["ds"].apply(lambda sec: datetime.fromtimestamp(sec))
    df_train["y"] = df_train["y"].astype(float)
    # histogram_quantile returns NaN for intervals with no traffic
    df_train = df_train.dropna()
    return df_train


def train_model(df_train):
    """Prophet with a fixed flat trend line and no seasonality (as in the notebook)."""
    model = Prophet(
        interval_width=0.95,
        growth="flat",
        yearly_seasonality=False,
        weekly_seasonality=False,
        daily_seasonality=False,
    )
    model.fit(df_train)
    return model


def get_latest_datapoints():
    """Instant query -> {service: (timestamp, value)} for every service with data."""
    r = requests.get(QUERY_URL, params={"query": LATEST_QUERY}, timeout=10)
    r.raise_for_status()
    prom = r.json()
    points = {}
    for result in prom["data"]["result"]:
        service = result["metric"].get("destination_canonical_service")
        points[service] = (result["value"][0], result["value"][1])
    return points


def detect_anomaly(model, sample_time, val):
    """Build the 1-record dataframe, predict, and flag an anomaly (notebook logic)."""
    df = pd.DataFrame(columns=["ds", "y"])
    new_row = {"ds": sample_time, "y": val}
    df.loc[0] = new_row
    df["y"] = df["y"].astype(float)
    df["ds"] = df["ds"].apply(lambda sec: datetime.fromtimestamp(sec))

    forecast = model.predict(df)
    performance = pd.merge(df, forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]], on="ds")
    performance["anomaly"] = performance.apply(
        lambda rows: 1 if ((float(rows.y) > rows.yhat_upper)) else 0, axis=1
    )
    return performance.iloc[0]


def main():
    services = list_services()
    print(f"Training {len(services)} models from {TRAINING_DIR}", flush=True)

    models = {}
    for service in services:
        df_train = load_training_data(service)
        models[service] = train_model(df_train)
        print(f"  {service:24s} {len(df_train):5d} rows  "
              f"({df_train['ds'].min()} -> {df_train['ds'].max()})", flush=True)

    # On-going record of each cycle's anomaly (0/1) and incident (0/1), per service
    history = {s: pd.DataFrame(columns=HISTORY_COLUMNS) for s in services}

    start_http_server(METRICS_PORT)
    print(f"Serving metrics on :{METRICS_PORT}/metrics", flush=True)
    print(f"Monitoring every {POLL_INTERVAL}s with query:\n  {LATEST_QUERY}\n", flush=True)

    while True:
        try:
            points = get_latest_datapoints()
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"{datetime.now()}  Prometheus query failed: {e}", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        now = datetime.now()
        summary = []
        for service in services:
            hist = history[service]
            sample_time, val = points.get(service, (now.timestamp(), "NaN"))

            if math.isnan(float(val)):
                # No requests in the last minute: nothing to evaluate, not an anomaly
                row = {"ds": datetime.fromtimestamp(sample_time), "y": float("nan"), "yhat": float("nan"),
                       "yhat_lower": float("nan"), "yhat_upper": float("nan"), "anomaly": 0}
            else:
                row = detect_anomaly(models[service], sample_time, val).to_dict()
            anomaly_count = int(row["anomaly"])

            # Look at the last INCIDENT_WINDOW entries, including this one
            last_anomalies = list(hist["anomaly"].tail(INCIDENT_WINDOW - 1)) + [anomaly_count]
            anomaly_last4_count = int(sum(last_anomalies))
            incident = 1 if anomaly_last4_count == INCIDENT_WINDOW else 0

            hist.loc[len(hist)] = [row["ds"], row["y"], row["yhat"], row["yhat_lower"],
                                   row["yhat_upper"], anomaly_count, incident]
            history[service] = hist.tail(HISTORY_ROWS).reset_index(drop=True)

            g.labels(service, "anomaly").set(anomaly_count)  # 0 or 1 anomaly
            if anomaly_last4_count == INCIDENT_WINDOW:
                g.labels(service, "incident").set(1)  # we're inside incident
            else:
                g.labels(service, "incident").set(0)  # no current incident

            summary.append({"service": service, "y": row["y"], "yhat": row["yhat"],
                            "yhat_upper": row["yhat_upper"], "anomaly": anomaly_count,
                            f"last{INCIDENT_WINDOW}": anomaly_last4_count, "incident": incident})

        summary_df = pd.DataFrame(summary).set_index("service")
        in_incident = summary_df.index[summary_df["incident"] == 1].tolist()
        print(f"\n=== {now:%Y-%m-%d %H:%M:%S}  anomalies={int(summary_df['anomaly'].sum())}  "
              f"incidents={len(in_incident)} {in_incident if in_incident else ''} ===", flush=True)
        print(summary_df.round(2), flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
