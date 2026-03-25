"""Tests for models and data structures."""

import re
import pytest
from pr_generator.models import (
    AppConfig, CycleResult, ProviderConfig, RuleResult, ScanRule,
)


def test_provider_config_is_immutable():
    cfg = ProviderConfig(name="github", enabled=True)
    with pytest.raises(Exception):
        cfg.name = "bitbucket"  # type: ignore[misc]


def test_app_config_is_immutable():
    cfg = AppConfig(
        scan_frequency=300,
        log_level="INFO",
        log_format="text",
        dry_run=False,
        health_port=8080,
        providers={},
        rules=[],
    )
    with pytest.raises(Exception):
        cfg.dry_run = True  # type: ignore[misc]


def test_scan_rule_destinations_default_empty():
    rule = ScanRule(pattern=".*", compiled=re.compile(".*"))
    assert rule.destinations == {}


def test_rule_result_defaults():
    r = RuleResult(rule_pattern="x", provider="github", destination="main")
    assert r.processed == 0
    assert r.created == 0
    assert r.skipped_existing == 0
    assert r.simulated == 0
    assert r.errors == 0


def test_cycle_result_aggregation():
    r1 = RuleResult("p1", "github", "main", processed=3, created=1, skipped_existing=2)
    r2 = RuleResult("p2", "bitbucket", "nonpro", processed=5, created=3, errors=1)
    cycle = CycleResult(cycle_id=1, rule_results=[r1, r2])
    assert sum(r.processed for r in cycle.rule_results) == 8
    assert sum(r.created for r in cycle.rule_results) == 4
    assert sum(r.errors for r in cycle.rule_results) == 1
