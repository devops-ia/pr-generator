# Prometheus Metrics — Design Spec

Date: 2026-05-24
Status: approved

## Summary

Add a `/metrics` endpoint to the existing health HTTP server (port 8080) exposing
Prometheus-compatible metrics. The Python app uses `prometheus-client` via a
`PrGeneratorMetrics` class (dependency-injected, registry-isolated). The Helm chart
adds pod annotations for plain Prometheus and an optional `ServiceMonitor` for
Prometheus Operator.

---

## 1. Architecture

### New files

| File | Purpose |
|------|---------|
| `src/pr_generator/metrics.py` | `PrGeneratorMetrics` class owning all metric objects |
| `tests/test_metrics.py` | Full test suite for metrics class and health `/metrics` endpoint |
| `charts/pr-generator/templates/servicemonitor.yaml` | Optional ServiceMonitor CRD |

### Modified files

| File | Change |
|------|--------|
| `src/pr_generator/health.py` | Add `/metrics` path; inject `PrGeneratorMetrics` via closure subclass |
| `src/pr_generator/__main__.py` | Instantiate `PrGeneratorMetrics` once at startup; pass to `start_health_server` and `scan_cycle` |
| `src/pr_generator/scanner.py` | Accept `metrics: PrGeneratorMetrics \| None = None`; time cycle; call `record_cycle()` |
| `requirements.txt` + `pyproject.toml` | Add `prometheus-client>=0.21.0` |
| `charts/pr-generator/values.yaml` | Add `metrics` section |
| `charts/pr-generator/templates/deployment.yaml` | Merge pod annotations when `metrics.enabled` |
| `charts/pr-generator/values.schema.json` | Add `metrics` object schema |
| `charts/pr-generator/Chart.yaml` | Bump `1.3.0 → 1.4.0` |

### Data flow

```
__main__.py
  ├── PrGeneratorMetrics(registry=REGISTRY)  ← instantiated once
  ├── start_health_server(..., metrics=m)    → /metrics returns generate_latest()
  └── loop:
        rules = _resolve_rules(...)
        m.record_annotation_rules(len(annotation_rules))
        result = scan_cycle(..., metrics=m)  → record_cycle() at end of cycle
```

`PrGeneratorMetrics` has no imports from the provider or scanner layer.
No circular dependencies.

---

## 2. Metrics Catalog

All metric names are prefixed `pr_generator_`.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `pr_generator_scan_cycles_total` | Counter | — | Scan cycles completed |
| `pr_generator_scan_duration_seconds` | Histogram | — | Duration per cycle. Buckets: .1, .5, 1, 5, 10, 30, 60 |
| `pr_generator_last_scan_timestamp_seconds` | Gauge | — | Unix timestamp of last completed cycle |
| `pr_generator_prs_created_total` | Counter | `provider` | PRs opened |
| `pr_generator_prs_skipped_total` | Counter | `provider` | PRs skipped (already open) |
| `pr_generator_prs_simulated_total` | Counter | `provider` | PRs simulated (`dry_run: true`) |
| `pr_generator_scan_errors_total` | Counter | `provider` | Errors during branch fetch or PR creation |
| `pr_generator_rules_active` | Gauge | — | Rules active in the current cycle |
| `pr_generator_annotation_rules_discovered` | Gauge | — | Rules discovered from ArgoCD annotations in last cycle |

**Label cardinality:** `provider` takes the key name from `config.providers` (e.g. `github`,
`my-bitbucket`). Fixed and low cardinality — safe.

**Histogram choice over Summary:** Histograms allow cross-instance aggregation via
`histogram_quantile()` in PromQL, consistent with Prometheus best practices.

---

## 3. `PrGeneratorMetrics` class interface

```python
class PrGeneratorMetrics:
    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None: ...

    def record_cycle(self, result: CycleResult, duration: float) -> None:
        """Increment counters and update gauges from a completed CycleResult."""

    def record_annotation_rules(self, count: int) -> None:
        """Update annotation_rules_discovered gauge."""

    def generate_latest(self) -> bytes:
        """Return Prometheus text exposition format."""
```

`record_cycle()` iterates `result.rule_results` and increments per-provider counters.
`generate_latest()` delegates to `prometheus_client.generate_latest(self._registry)`.

---

## 4. `health.py` extension

The existing `_HealthHandler` is extended with one new branch in `do_GET`:

```python
elif self.path == "/metrics":
    if self.metrics is not None:
        body = self.metrics.generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.end_headers()
        self.wfile.write(body)
    else:
        self._write(404, "metrics not enabled")
```

`metrics` is injected into the handler class via the same closure-built subclass
pattern used for `stop_event` and `ready_event`. `start_health_server` signature
gains `metrics: PrGeneratorMetrics | None = None`.

---

## 5. `scanner.py` extension

`scan_cycle()` gains `metrics: PrGeneratorMetrics | None = None`. Timing uses
`time.monotonic()` bracketing the full cycle. At the end:

```python
if metrics is not None:
    metrics.record_cycle(result, duration=time.monotonic() - t_start)
```

No metrics import at module level — `PrGeneratorMetrics` is imported inside
`TYPE_CHECKING` guard for the type hint, avoiding a hard dependency in the scanner.

---

## 6. Helm chart

### `values.yaml`

```yaml
metrics:
  enabled: true
  podAnnotations:
    prometheus.io/scrape: "true"
    prometheus.io/path: "/metrics"
    prometheus.io/port: "8080"
  serviceMonitor:
    enabled: false
    interval: 30s
    scrapeTimeout: 10s
    labels: {}
    namespace: ""
```

### `servicemonitor.yaml`

Conditional on `metrics.enabled && metrics.serviceMonitor.enabled`.
Points to the existing `http` port (8080) on the chart Service.
Namespace defaults to `Release.Namespace`.

### `deployment.yaml`

When `metrics.enabled`, merge `metrics.podAnnotations` into pod annotations
(in addition to the existing `checksum/config` annotation).

### Schema

`values.schema.json` adds `metrics` object with:
- `enabled: boolean`
- `podAnnotations: object` (additionalProperties: string)
- `serviceMonitor: object` with `enabled`, `interval`, `scrapeTimeout`, `labels`, `namespace`

---

## 7. Tests

### `tests/test_metrics.py`

**`TestPrGeneratorMetrics`:**
- Each test instantiates `PrGeneratorMetrics(registry=CollectorRegistry())` — no global state pollution
- `record_cycle()` with multi-provider result increments all counters correctly
- `record_cycle()` with empty result leaves counters at zero
- `record_annotation_rules(5)` sets gauge to 5; calling again with 3 sets to 3
- `generate_latest()` output contains expected metric names

**`tests/test_health.py`** (new or extension):
- `GET /metrics` returns 200 with `Content-Type: text/plain; version=0.0.4`
- `GET /metrics` with `metrics=None` returns 404
- Existing health endpoints unaffected

### Isolation pattern

```python
def test_record_cycle_creates_pr():
    registry = CollectorRegistry()
    m = PrGeneratorMetrics(registry=registry)
    result = CycleResult(cycle_id=1, rule_results=[
        RuleResult(rule_pattern=".*", provider="github",
                   destination="main", created=2, errors=0, ...)
    ])
    m.record_cycle(result, duration=1.5)
    output = m.generate_latest().decode()
    assert 'pr_generator_prs_created_total{provider="github"} 2.0' in output
```

---

## 8. Dependency

```
prometheus-client>=0.21.0
```

Added to both `requirements.txt` and `pyproject.toml` `[project.dependencies]`.
`prometheus-client` is a pure-Python package with no C extensions — no build
requirements, compatible with the existing Alpine-based Docker image.

---

## Out of scope

- Push gateway support
- Custom histogram bucket configuration via values
- Per-rule-pattern label (cardinality risk with dynamic annotation rules)
- Alerting rules / PrometheusRule CRD
