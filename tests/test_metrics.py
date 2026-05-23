"""Tests for PrGeneratorMetrics and the /metrics health endpoint."""

from __future__ import annotations

import time
import urllib.request
from threading import Event

import pytest
from prometheus_client import CollectorRegistry

from pr_generator.health import start_health_server
from pr_generator.metrics import PrGeneratorMetrics
from pr_generator.models import CycleResult, RuleResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_metrics() -> PrGeneratorMetrics:
    """Return a metrics instance with an isolated registry."""
    return PrGeneratorMetrics(registry=CollectorRegistry())


def _output(m: PrGeneratorMetrics) -> str:
    return m.generate_latest().decode()


def _make_cycle(
    *,
    cycle_id: int = 1,
    provider: str = "github",
    created: int = 0,
    skipped: int = 0,
    simulated: int = 0,
    errors: int = 0,
) -> CycleResult:
    return CycleResult(
        cycle_id=cycle_id,
        rule_results=[
            RuleResult(
                rule_pattern="^image-updater/.*",
                provider=provider,
                destination="main",
                created=created,
                skipped_existing=skipped,
                simulated=simulated,
                errors=errors,
            )
        ],
    )


# ---------------------------------------------------------------------------
# PrGeneratorMetrics — unit tests
# ---------------------------------------------------------------------------

class TestPrGeneratorMetrics:
    def test_scan_cycles_increments(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(), duration=1.0)
        assert "pr_generator_scan_cycles_total 1.0" in _output(m)

    def test_scan_cycles_accumulates(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(cycle_id=1), duration=0.5)
        m.record_cycle(_make_cycle(cycle_id=2), duration=0.5)
        assert "pr_generator_scan_cycles_total 2.0" in _output(m)

    def test_prs_created_by_provider(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(created=3), duration=1.0)
        assert 'pr_generator_prs_created_total{provider="github"} 3.0' in _output(m)

    def test_prs_skipped_by_provider(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(skipped=2), duration=1.0)
        assert 'pr_generator_prs_skipped_total{provider="github"} 2.0' in _output(m)

    def test_prs_simulated_by_provider(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(simulated=1), duration=1.0)
        assert 'pr_generator_prs_simulated_total{provider="github"} 1.0' in _output(m)

    def test_scan_errors_by_provider(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(errors=4), duration=1.0)
        assert 'pr_generator_scan_errors_total{provider="github"} 4.0' in _output(m)

    def test_multiple_providers_tracked_independently(self) -> None:
        m = _make_metrics()
        result = CycleResult(
            cycle_id=1,
            rule_results=[
                RuleResult("p1", provider="github", destination="main", created=2),
                RuleResult("p2", provider="bitbucket", destination="main", created=5),
            ],
        )
        m.record_cycle(result, duration=1.0)
        out = _output(m)
        assert 'pr_generator_prs_created_total{provider="github"} 2.0' in out
        assert 'pr_generator_prs_created_total{provider="bitbucket"} 5.0' in out

    def test_empty_result_does_not_raise(self) -> None:
        m = _make_metrics()
        empty = CycleResult(cycle_id=1, rule_results=[])
        m.record_cycle(empty, duration=0.1)
        assert "pr_generator_scan_cycles_total 1.0" in _output(m)

    def test_zero_counters_not_emitted_before_labels_used(self) -> None:
        m = _make_metrics()
        out = _output(m)
        # Label series are only emitted after first use
        assert 'provider="github"' not in out

    def test_scan_duration_histogram_present(self) -> None:
        m = _make_metrics()
        m.record_cycle(_make_cycle(), duration=2.5)
        out = _output(m)
        assert "pr_generator_scan_duration_seconds_bucket" in out
        assert "pr_generator_scan_duration_seconds_sum" in out

    def test_last_scan_timestamp_updated(self) -> None:
        m = _make_metrics()
        before = time.time()
        m.record_cycle(_make_cycle(), duration=0.1)
        after = time.time()
        out = _output(m)
        # Extract the gauge value from the output
        for line in out.splitlines():
            if line.startswith("pr_generator_last_scan_timestamp_seconds") and not line.startswith("#"):
                ts = float(line.split()[-1])
                assert before <= ts <= after
                break
        else:
            pytest.fail("pr_generator_last_scan_timestamp_seconds not found in output")

    def test_annotation_rules_discovered_gauge(self) -> None:
        m = _make_metrics()
        m.record_annotation_rules(5)
        assert "pr_generator_annotation_rules_discovered 5.0" in _output(m)

    def test_annotation_rules_gauge_updates_on_second_call(self) -> None:
        m = _make_metrics()
        m.record_annotation_rules(5)
        m.record_annotation_rules(3)
        assert "pr_generator_annotation_rules_discovered 3.0" in _output(m)

    def test_annotation_rules_zero(self) -> None:
        m = _make_metrics()
        m.record_annotation_rules(0)
        assert "pr_generator_annotation_rules_discovered 0.0" in _output(m)

    def test_rules_active_gauge(self) -> None:
        m = _make_metrics()
        m.rules_active.set(7)
        assert "pr_generator_rules_active 7.0" in _output(m)

    def test_generate_latest_returns_bytes(self) -> None:
        m = _make_metrics()
        assert isinstance(m.generate_latest(), bytes)

    def test_isolated_registries_do_not_share_state(self) -> None:
        m1 = _make_metrics()
        m2 = _make_metrics()
        m1.record_cycle(_make_cycle(created=10), duration=1.0)
        # m2 should not see m1's data
        assert 'pr_generator_prs_created_total{provider="github"} 10.0' not in _output(m2)


# ---------------------------------------------------------------------------
# /metrics endpoint — integration tests via live health server
# ---------------------------------------------------------------------------

_METRICS_PORT = 18082


@pytest.fixture(scope="module")
def metrics_server():
    stop = Event()
    m = PrGeneratorMetrics(registry=CollectorRegistry())
    server, ready = start_health_server(_METRICS_PORT, stop, metrics=m)
    time.sleep(0.1)
    yield stop, ready, m
    stop.set()
    server.shutdown()


@pytest.fixture(scope="module")
def health_server_no_metrics():
    stop = Event()
    server, ready = start_health_server(_METRICS_PORT + 1, stop, metrics=None)
    time.sleep(0.1)
    yield stop, ready
    stop.set()
    server.shutdown()


def _get(port: int, path: str) -> tuple[int, str]:
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, ""


class TestMetricsEndpoint:
    def test_metrics_returns_200(self, metrics_server) -> None:
        status, _ = _get(_METRICS_PORT, "/metrics")
        assert status == 200

    def test_metrics_content_type(self, metrics_server) -> None:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{_METRICS_PORT}/metrics", timeout=2)
        assert "text/plain" in resp.headers.get("Content-Type", "")

    def test_metrics_body_contains_pr_generator_prefix(self, metrics_server) -> None:
        _, body = _get(_METRICS_PORT, "/metrics")
        assert "pr_generator_" in body

    def test_metrics_reflects_recorded_data(self, metrics_server) -> None:
        _, _, m = metrics_server
        m.record_annotation_rules(9)
        _, body = _get(_METRICS_PORT, "/metrics")
        assert "pr_generator_annotation_rules_discovered 9.0" in body

    def test_metrics_404_when_not_enabled(self, health_server_no_metrics) -> None:
        status, _ = _get(_METRICS_PORT + 1, "/metrics")
        assert status == 404

    def test_health_endpoints_unaffected(self, metrics_server) -> None:
        _, ready, _ = metrics_server
        ready.set()
        status, _ = _get(_METRICS_PORT, "/livez")
        assert status == 200
        status, _ = _get(_METRICS_PORT, "/readyz")
        assert status == 200
