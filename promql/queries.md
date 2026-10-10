# PromQL queries

All latency queries use the Istio standard metric `istio_request_duration_milliseconds_bucket`,
which the Envoy sidecars emit as a Prometheus histogram.

Two choices apply to every query below:

- **`reporter="source"`**: the latency is measured by the *caller's* sidecar. Istio injects the
  fault delay on the client side, so the destination sidecar inside shippingservice never sees it.
  With `reporter="destination"` the delay is invisible.
- **Custom millisecond buckets**: Istio's default buckets are too coarse around 1s. Every
  deployment in `kustomize/base/` sets this annotation:
  ```yaml
  sidecar.istio.io/statsHistogramBuckets: |
    {"istiocustom":[0.1,0.2,0.5,1,5,10,50,100,150,200,250,300,400,500,600,700,800,900,1000,1500,2000,3000]}
  ```

---

## 1. p99 shipping latency, split by caller (frontend vs. checkoutservice)

`sum by (le, source_canonical_service)` keeps the two call paths as separate series in a **single**
query:

```promql
histogram_quantile(0.99,
  sum by (le, source_canonical_service) (
    rate(istio_request_duration_milliseconds_bucket{
      destination_canonical_service="shippingservice",
      reporter="source"
    }[5m])
  )
)
```

**Table view, one value per minute for the last 30 minutes:** add a subquery range to the same
expression:

```promql
histogram_quantile(0.99,
  sum by (le, source_canonical_service) (
    rate(istio_request_duration_milliseconds_bucket{
      destination_canonical_service="shippingservice",
      reporter="source"
    }[5m])
  )
)[30m:1m]
```

| State | frontend → shipping | checkout → shipping |
|---|---|---|
| Healthy | tens of ms | tens of ms |
| `01-delay-checkout-to-shipping.yaml` applied | unchanged | **~1000 ms** |

## 2. p99 checkout → shipping latency, split by checkoutservice pod

With two checkoutservice replicas and only one labelled `fault=delay`, group by `pod` (the
scraped source pod) to see the delayed and healthy replicas side by side:

```promql
histogram_quantile(0.99,
  sum by (le, pod) (
    rate(istio_request_duration_milliseconds_bucket{
      destination_canonical_service="shippingservice",
      source_canonical_service="checkoutservice",
      reporter="source"
    }[5m])
  )
)
```

Add `[30m:1m]` for the per-minute table, as in query 1.

## 3. Grafana: histogram (Bar gauge) and heatmap

Grafana draws the histogram itself, so drop the `histogram_quantile()` wrapper and any `[x:y]`
subrange:

```promql
sum by (le, source_canonical_service) (
  rate(istio_request_duration_milliseconds_bucket{
    destination_canonical_service="shippingservice",
    source_canonical_service=~"checkoutservice|frontend",
    reporter="source"
  }[5m])
)
```

| Panel | Visualization | Format | Type |
|---|---|---|---|
| Latency histogram | **Bar gauge** | Heatmap | Instant |
| Latency over time (extra credit) | **Heatmap** | Heatmap | Range |

The heatmap panel uses the per-pod checkout → shipping series. While the single-pod delay is
applied and removed, the chart switches between one latency band and two (~50 ms and ~1000 ms).

The exported dashboard is in [`../grafana/shipping-latency-dashboard.json`](../grafana/shipping-latency-dashboard.json).

## 4. Prometheus HTTP API (input for anomaly detection)

The Prophet notebook pulls the p95 series over the HTTP API instead of the UI:

```bash
kubectl -n istio-system port-forward svc/prometheus 9090:9090 &

curl -sG http://localhost:9090/api/v1/query --data-urlencode \
 "query=histogram_quantile(0.95, sum by (le) (increase(istio_request_duration_milliseconds_bucket{destination_canonical_service='shippingservice', reporter='source'}[1m])))[1h:30s]"
```

The response is a `matrix` of `[timestamp, value]` pairs. One sample of it is saved in
[`../anomaly-detection/shipping_responsetime.json`](../anomaly-detection/shipping_responsetime.json).
