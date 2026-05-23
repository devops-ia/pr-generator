"""Prometheus metrics for pr-generator.

All metrics are registered into a :class:`~prometheus_client.CollectorRegistry`.
Production code uses the default global registry; tests pass an isolated
``CollectorRegistry()`` to prevent cross-test pollution.
"""

from __future__ import annotations

import time as _time_module
from typing import TYPE_CHECKING

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

if TYPE_CHECKING:
    from pr_generator.models import CycleResult

#: Expose the Prometheus content-type so callers do not need to import
#: ``prometheus_client`` directly.
METRICS_CONTENT_TYPE: str = CONTENT_TYPE_LATEST

_HISTOGRAM_BUCKETS = (0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0)


class PrGeneratorMetrics:
    """Container for all Prometheus metrics emitted by pr-generator.

    Each instance owns a separate set of metric objects registered into
    *registry*.  Pass the global ``REGISTRY`` in production and a fresh
    ``CollectorRegistry()`` in every test to avoid state leaking between tests.

    Args:
        registry: Prometheus collector registry.  Defaults to the global
            :data:`~prometheus_client.REGISTRY`.

    Example:
        >>> from prometheus_client import CollectorRegistry
        >>> m = PrGeneratorMetrics(registry=CollectorRegistry())
        >>> m.record_annotation_rules(3)
    """

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self._registry = registry

        self.scan_cycles = Counter(
            "pr_generator_scan_cycles_total",
            "Total number of scan cycles completed.",
            registry=registry,
        )
        self.scan_duration = Histogram(
            "pr_generator_scan_duration_seconds",
            "Duration of each scan cycle in seconds.",
            buckets=_HISTOGRAM_BUCKETS,
            registry=registry,
        )
        self.last_scan_timestamp = Gauge(
            "pr_generator_last_scan_timestamp_seconds",
            "Unix timestamp of the last completed scan cycle.",
            registry=registry,
        )
        self.prs_created = Counter(
            "pr_generator_prs_created_total",
            "Total PRs opened, labelled by provider.",
            labelnames=["provider"],
            registry=registry,
        )
        self.prs_skipped = Counter(
            "pr_generator_prs_skipped_total",
            "Total PRs skipped because one already exists, labelled by provider.",
            labelnames=["provider"],
            registry=registry,
        )
        self.prs_simulated = Counter(
            "pr_generator_prs_simulated_total",
            "Total PRs simulated in dry-run mode, labelled by provider.",
            labelnames=["provider"],
            registry=registry,
        )
        self.scan_errors = Counter(
            "pr_generator_scan_errors_total",
            "Total errors during branch fetch or PR creation, labelled by provider.",
            labelnames=["provider"],
            registry=registry,
        )
        self.rules_active = Gauge(
            "pr_generator_rules_active",
            "Number of rules active in the current scan cycle.",
            registry=registry,
        )
        self.annotation_rules_discovered = Gauge(
            "pr_generator_annotation_rules_discovered",
            "Number of rules discovered from ArgoCD Application annotations in the last cycle.",
            registry=registry,
        )

    def record_cycle(self, result: CycleResult, duration: float) -> None:
        """Record metrics from a completed scan cycle.

        Increments per-provider counters for PRs created, skipped, simulated,
        and errors.  Updates the cycle counter, duration histogram, and the
        last-scan timestamp.

        Args:
            result: The :class:`~pr_generator.models.CycleResult` returned by
                :func:`~pr_generator.scanner.scan_cycle`.
            duration: Elapsed wall-clock time in seconds for the cycle.
        """
        self.scan_cycles.inc()
        self.scan_duration.observe(duration)
        self.last_scan_timestamp.set(_time_module.time())

        for rr in result.rule_results:
            provider = rr.provider
            if rr.created:
                self.prs_created.labels(provider=provider).inc(rr.created)
            if rr.skipped_existing:
                self.prs_skipped.labels(provider=provider).inc(rr.skipped_existing)
            if rr.simulated:
                self.prs_simulated.labels(provider=provider).inc(rr.simulated)
            if rr.errors:
                self.scan_errors.labels(provider=provider).inc(rr.errors)

    def record_annotation_rules(self, count: int) -> None:
        """Update the annotation-rules-discovered gauge.

        Args:
            count: Number of rules discovered from ArgoCD Application annotations
                in the current cycle.  Pass ``0`` when annotation discovery is
                disabled or no Applications are annotated.
        """
        self.annotation_rules_discovered.set(count)

    def generate_latest(self) -> bytes:
        """Render all metrics in Prometheus text exposition format.

        Returns:
            UTF-8 encoded payload in the ``text/plain; version=0.0.4`` format
            expected by Prometheus scrapers.
        """
        return generate_latest(self._registry)
