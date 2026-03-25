"""Tests for the scan cycle orchestrator."""

import re
from unittest.mock import MagicMock, patch

import pytest

from pr_generator.models import AppConfig, CycleResult, ProviderConfig, ScanRule
from pr_generator.scanner import scan_cycle


def _make_config(rules, providers, dry_run=False):
    return AppConfig(
        scan_frequency=60,
        log_level="DEBUG",
        log_format="text",
        dry_run=dry_run,
        health_port=8080,
        providers=providers,
        rules=rules,
    )


def _mock_provider(name: str, branches: list[str], existing_prs: set | None = None):
    prov = MagicMock()
    prov.name = name
    prov.get_branches.return_value = branches
    prov.check_existing_pr.side_effect = lambda src, dst: (src, dst) in (existing_prs or set())
    prov.create_pull_request.return_value = None
    prov.reset_cycle_cache.return_value = None
    return prov


class TestScanCycle:
    def test_creates_prs_for_matched_branches(self):
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", ["feature/a", "feature/b", "hotfix/c", "main"])
        config = _make_config([rule], {"github": MagicMock()})

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        assert prov.create_pull_request.call_count == 2
        prov.create_pull_request.assert_any_call("feature/a", "main")
        prov.create_pull_request.assert_any_call("feature/b", "main")
        assert result.rule_results[0].created == 2
        assert result.rule_results[0].processed == 2

    def test_skips_existing_prs(self):
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", ["feature/a"], existing_prs={("feature/a", "main")})
        config = _make_config([rule], {"github": MagicMock()})

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        prov.create_pull_request.assert_not_called()
        assert result.rule_results[0].skipped_existing == 1

    def test_dry_run_does_not_create_prs(self):
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", ["feature/a"])
        config = _make_config([rule], {"github": MagicMock()}, dry_run=True)

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        prov.create_pull_request.assert_not_called()
        assert result.rule_results[0].simulated == 1

    def test_destination_branch_excluded_from_matches(self):
        rule = ScanRule(
            pattern=".*",
            compiled=re.compile(".*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", ["main", "feature/a"])
        config = _make_config([rule], {"github": MagicMock()})

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        # "main" must be excluded; only "feature/a" should be processed
        assert result.rule_results[0].processed == 1

    def test_multiple_rules_processed(self):
        rule1 = ScanRule(
            pattern="nonpro/.*",
            compiled=re.compile("nonpro/.*"),
            destinations={"github": "develop"},
        )
        rule2 = ScanRule(
            pattern="pro/.*",
            compiled=re.compile("pro/.*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", ["nonpro/svc1", "pro/svc2", "unrelated"])
        config = _make_config([rule1, rule2], {"github": MagicMock()})

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        assert len(result.rule_results) == 2
        total_created = sum(r.created for r in result.rule_results)
        assert total_created == 2

    def test_provider_error_does_not_abort_other_rules(self):
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", ["feature/a"])
        prov.create_pull_request.side_effect = RuntimeError("API down")
        config = _make_config([rule], {"github": MagicMock()})

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        assert result.rule_results[0].errors == 1

    def test_unknown_provider_in_rule_is_skipped(self):
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"unknown_provider": "main"},
        )
        prov = _mock_provider("github", ["feature/a"])
        config = _make_config([rule], {"github": MagicMock()})

        # Should not raise; the rule simply has no matching active provider
        result = scan_cycle(config, {"github": prov}, cycle_id=1)
        assert result.rule_results == []

    def test_reset_cycle_cache_called_on_all_providers(self):
        rule = ScanRule(".*", re.compile(".*"), destinations={"github": "main"})
        prov = _mock_provider("github", [])
        config = _make_config([rule], {"github": MagicMock()})

        scan_cycle(config, {"github": prov}, cycle_id=1)

        prov.reset_cycle_cache.assert_called_once()

    def test_get_branches_error_returns_empty_branch_list(self):
        """If get_branches raises, the provider gets an empty branch list (no crash)."""
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"github": "main"},
        )
        prov = _mock_provider("github", [])
        prov.get_branches.side_effect = RuntimeError("API down")
        config = _make_config([rule], {"github": MagicMock()})

        result = scan_cycle(config, {"github": prov}, cycle_id=1)

        # No branches → no PRs created, but no exception raised either
        assert result.rule_results[0].processed == 0

    def test_process_rule_unexpected_exception_logged(self):
        """An exception raised outside _process_rule's inner try is caught by the futures loop."""
        rule = ScanRule(
            pattern="feature/.*",
            compiled=re.compile("feature/.*"),
            destinations={"github": "main"},
        )
        # Make compiled.match raise — this happens outside the inner try in _process_rule
        rule.compiled = MagicMock()
        rule.compiled.match.side_effect = ValueError("regex engine failure")
        prov = _mock_provider("github", ["feature/a"])
        config = _make_config([rule], {"github": MagicMock()})

        # scan_cycle should catch the exception and not propagate it
        result = scan_cycle(config, {"github": prov}, cycle_id=1)
        assert result is not None
        assert result.rule_results == []
