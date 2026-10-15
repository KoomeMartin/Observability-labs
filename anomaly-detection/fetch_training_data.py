"""
Pull p95 response-time training data for every Boutique service from Prometheus.

Runs the same query the monitor uses, once per service, over a fixed window
(1 point every 30s), and saves the raw Prometheus JSON response to
training_data/<service>_responsetime.json - the same format as
shipping_responsetime.json.

Choose an END time from before any fault injection so the models learn
normal behaviour only.

Usage:
    python fetch_training_data.py --end "2026-10-15 12:00" --hours 24
    python fetch_training_data.py --prometheus http://localhost:9090
"""

import argparse
import json
import math
import os
from datetime import datetime

import requests

SERVICES = [
    "frontend",
    "cartservice",
    "productcatalogservice",
    "currencyservice",
    "paymentservice",
    "shippingservice",
    "emailservice",
    "checkoutservice",
    "recommendationservice",
    "adservice",
]

QUERY = (
    "histogram_quantile( 0.95, sum by (le) (increase("
    "istio_request_duration_milliseconds_bucket{{destination_canonical_service='{service}', "
    "reporter='source'}}[1m])))"
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prometheus", default="http://localhost:9090")
    parser.add_argument("--end", default=None,
                        help='end of the training window, local time "YYYY-MM-DD HH:MM" (default: now)')
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--step", default="30s")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "training_data"))
    args = parser.parse_args()

    end = datetime.strptime(args.end, "%Y-%m-%d %H:%M") if args.end else datetime.now()
    end_ts = int(end.timestamp())
    start_ts = int(end_ts - args.hours * 3600)
    os.makedirs(args.out, exist_ok=True)

    print(f"Window: {datetime.fromtimestamp(start_ts)} -> {datetime.fromtimestamp(end_ts)}  step {args.step}")
    for service in SERVICES:
        r = requests.get(
            f"{args.prometheus}/api/v1/query_range",
            params={"query": QUERY.format(service=service), "start": start_ts, "end": end_ts, "step": args.step},
            timeout=60,
        )
        r.raise_for_status()
        prom = r.json()
        result = prom["data"]["result"]
        if not result:
            print(f"  {service:24s} no data, skipped")
            continue

        values = [float(v) for _, v in result[0]["values"]]
        valid = [v for v in values if not math.isnan(v)]
        path = os.path.join(args.out, f"{service}_responsetime.json")
        with open(path, "w") as f:
            json.dump(prom, f)
        summary = f"median {sorted(valid)[len(valid) // 2]:8.2f} ms  max {max(valid):8.2f} ms" if valid else ""
        print(f"  {service:24s} {len(values):5d} points ({len(valid):5d} non-NaN)  {summary}  -> {path}")


if __name__ == "__main__":
    main()
