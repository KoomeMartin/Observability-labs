"""
Lab 3 anomaly / incident monitor for a single service (shippingservice).

1. Train a Prophet model (flat trend, no seasonality) on the p95 response-time
   history pulled from Prometheus (shipping_responsetime.json by default).
2. Every cycle, pull a single datapoint (p95 over the last minute, instant
   query with no [x:y] sub-query), build a 1-record dataframe, predict, and flag
   an anomaly if the actual value is above the upper bound of the 95% interval.
3. Keep a dataframe of anomaly results; if the last 4 entries are all
   anomalies, the service is considered to be in an incident.
4. Publish both values as one Prometheus gauge with labels service / kind.

Configuration (environment variables):
    PROMETHEUS_URL     Prometheus base URL          (default http://prometheus.istio-system:9090;
                       use http://localhost:9090 when running locally)
    SERVICE_NAME       destination_canonical_service (default shippingservice)
    TRAINING_SOURCE    "file" or "prometheus"        (default file)
    TRAINING_FILE      JSON file with query result   (default shipping_responsetime.json)
    TRAINING_RANGE     sub-query range when TRAINING_SOURCE=prometheus (default 12h:30s)
    POLL_INTERVAL      seconds between checks        (default 60)
    METRICS_PORT       port for the /metrics server  (default 8099)
    INCIDENT_WINDOW    consecutive anomalies needed  (default 4)
"""

import json
import logging
import os
import time
from datetime import datetime

import pandas as pd
import requests
from prometheus_client import Gauge, start_http_server
from prophet import Prophet

PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus.istio-system:9090")
SERVICE_NAME = os.getenv("SERVICE_NAME", "shippingservice")
TRAINING_SOURCE = os.getenv("TRAINING_SOURCE", "file")
TRAINING_FILE = os.getenv(
    "TRAINING_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "shipping_responsetime.json"),
)
TRAINING_RANGE = os.getenv("TRAINING_RANGE", "12h:30s")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
METRICS_PORT = int(os.getenv("METRICS_PORT", "8099"))
INCIDENT_WINDOW = int(os.getenv("INCIDENT_WINDOW", "4"))

QUERY_URL = f"{PROMETHEUS_URL}/api/v1/query"

# p95 response time over the last minute. No [x:y] sub-query at the end, so
# Prometheus returns a single value (resultType "vector").
LATEST_QUERY = (
    "histogram_quantile( 0.95, sum by (le) (increase("
    f"istio_request_duration_milliseconds_bucket{{destination_canonical_service='{SERVICE_NAME}', "
    "reporter='source'}[1m])))"
)

# Same query with a sub-query, used only when training from live Prometheus data.
TRAINING_QUERY = LATEST_QUERY + f"[{TRAINING_RANGE}]"

# Quiet Prophet / cmdstanpy chatter so the console shows the monitor output.
logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
logging.getLogger("prophet").setLevel(logging.WARNING)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 20)

# Single metric name, labelled by service and kind ("anomaly" or "incident").
g = Gauge(
    "lab3_anomalies_and_incidents",
    "Gauge for tracking anomalies and incidents across all services",
    ["service", "kind"],
)


def load_training_data():
    """Return the training dataframe (ds, y) from the JSON file or Prometheus."""
    if TRAINING_SOURCE == "prometheus":
        r = requests.get(QUERY_URL, params={"query": TRAINING_QUERY}, timeout=30)
        r.raise_for_status()
        prom = r.json()
        print(f"Training on Prometheus data: {TRAINING_QUERY}", flush=True)
    else:
        with open(TRAINING_FILE) as f:
            prom = json.load(f)
        print(f"Training on file: {TRAINING_FILE}", flush=True)

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


def get_latest_datapoint():
    """Instant query -> 1-record dataframe (ds, y), or None if no data."""
    r = requests.get(QUERY_URL, params={"query": LATEST_QUERY}, timeout=10)
    r.raise_for_status()
    prom = r.json()
    result = prom["data"]["result"]
    if not result:
        return None

    sample_time = result[0]["value"][0]
    val = result[0]["value"][1]

    df = pd.DataFrame(columns=["ds", "y"])
    new_row = {"ds": sample_time, "y": val}
    df.loc[0] = new_row
    df["y"] = df["y"].astype(float)
    df["ds"] = df["ds"].apply(lambda sec: datetime.fromtimestamp(sec))
    if df["y"].isna().any():
        # NaN = no requests in the last minute, nothing to evaluate
        return None
    return df


def detect_anomaly(model, df):
    """Predict for the 1-record dataframe and flag an anomaly (notebook logic)."""
    forecast = model.predict(df)
    performance = pd.merge(df, forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]], on="ds")
    performance["anomaly"] = performance.apply(
        lambda rows: 1 if ((float(rows.y) > rows.yhat_upper)) else 0, axis=1
    )
    return performance


def main():
    df_train = load_training_data()
    print(f"Training rows: {len(df_train)}  ({df_train['ds'].min()} -> {df_train['ds'].max()})", flush=True)
    model = train_model(df_train)

    start_http_server(METRICS_PORT)
    print(f"Serving metrics on :{METRICS_PORT}/metrics", flush=True)
    print(f"Monitoring {SERVICE_NAME} every {POLL_INTERVAL}s with query:\n  {LATEST_QUERY}\n", flush=True)

    # On-going record of each cycle's anomaly (0/1) and incident (0/1) result
    anomalies_df = pd.DataFrame(
        columns=["ds", "y", "yhat", "yhat_lower", "yhat_upper", "anomaly", "incident"]
    )

    while True:
        try:
            df = get_latest_datapoint()
        except (requests.RequestException, KeyError, ValueError) as e:
            print(f"{datetime.now()}  Prometheus query failed: {e}", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        if df is None:
            print(f"{datetime.now()}  No data for {SERVICE_NAME} in the last minute, skipping", flush=True)
            time.sleep(POLL_INTERVAL)
            continue

        performance = detect_anomaly(model, df)
        row = performance.iloc[0]
        anomaly_count = int(row["anomaly"])

        # Look at the last INCIDENT_WINDOW entries, including this one
        last_anomalies = list(anomalies_df["anomaly"].tail(INCIDENT_WINDOW - 1)) + [anomaly_count]
        anomaly_last4_count = int(sum(last_anomalies))
        incident = 1 if anomaly_last4_count == INCIDENT_WINDOW else 0

        anomalies_df.loc[len(anomalies_df)] = [
            row["ds"], row["y"], row["yhat"], row["yhat_lower"], row["yhat_upper"],
            anomaly_count, incident,
        ]
        # Bound memory: only recent history is needed
        anomalies_df = anomalies_df.tail(1440).reset_index(drop=True)

        g.labels(SERVICE_NAME, "anomaly").set(anomaly_count)  # 0 or 1 anomaly
        if anomaly_last4_count == INCIDENT_WINDOW:
            g.labels(SERVICE_NAME, "incident").set(1)  # we're inside incident
        else:
            g.labels(SERVICE_NAME, "incident").set(0)  # no current incident

        print(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  service={SERVICE_NAME}  "
              f"anomaly={anomaly_count}  last{INCIDENT_WINDOW}={anomaly_last4_count}  "
              f"incident={incident} ===", flush=True)
        print(anomalies_df, flush=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
