# PromQL queries

All latency queries use the Istio standard metric `istio_request_duration_milliseconds_bucket`,
which the Envoy sidecars emit as a Prometheus histogram.

Two choices apply to every query below:

- **`reporter="source"`**: the latency is measured by the *caller's* sidecar. Istio injects the
  fault delay on the client side, so the destination sidecar inside shippingservice never sees it.
  With `reporter="destination"` the delay is invisible.
- **Custom millisecond buckets**: Istio reports latency in ms, so the buckets need to be in ms
  too. Every deployment in [`../kustomize/base/`](../kustomize/base/) sets this annotation:
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

Measured results (from the Lab 2 report):

| State | frontend → shipping | checkout → shipping |
|---|---|---|
| Healthy | 2 ms | ≈3–4.7 ms |
| `01-delay-checkout-to-shipping.yaml` applied | 2 ms (unchanged) | **≈995–998 ms** |

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

Measured: **999 ms** for the labelled pod (`…-qhc9n`) and **≈8.3 ms** for the other replica (`…-xq89n`).

## 3. Grafana: histogram (Bar gauge) and heatmap

Grafana draws the histogram itself, so drop the `histogram_quantile()` wrapper and any `[x:y]`
subrange and give it the raw bucket rates.

**Bar gauge histogram (both callers, delay on the checkout path):**

```promql
sum by (le) (
  rate(istio_request_duration_milliseconds_bucket{
    destination_canonical_service="shippingservice",
    source_canonical_service=~"checkoutservice|frontend",
    reporter="source"
  }[5m])
)
```

Because the query sums over `le` only, both call paths go into one distribution, which ends up
**bimodal**: undelayed calls pile up in the ~1 ms bucket and delayed checkout calls in the
1000 ms / +Inf buckets. To draw each caller as its own histogram instead, use
`sum by (le, source_canonical_service)`.

**Heatmap (extra credit, calls from checkoutservice with one pod delayed):**

```promql
sum by (le) (
  rate(istio_request_duration_milliseconds_bucket{
    destination_canonical_service="shippingservice",
    source_canonical_service="checkoutservice",
    reporter="source"
  }[5m])
)
```

| Panel | Visualization | Format | Type |
|---|---|---|---|
| Latency histogram | **Bar gauge** | Heatmap | Instant |
| Latency over time | **Heatmap** | Heatmap | Range |

*Format = Heatmap* tells Grafana to turn the cumulative `le` buckets into per-bucket counts. With
the single-pod delay applied, the heatmap shows two bands (< 5 ms and ~1000 ms). After the rule
is deleted, only the low band is left.

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
