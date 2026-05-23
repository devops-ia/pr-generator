"""Tests for annotation_discovery module."""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

from pr_generator.annotation_discovery import AnnotationDiscoveryClient, merge_rules
from pr_generator.models import ScanRule


# ------------------------------------------------------------------ helpers --

def _make_app(name: str, namespace: str, annotations: dict) -> dict:
    """Build a minimal ArgoCD Application dict."""
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "annotations": annotations,
        },
    }


def _make_client(items: list[dict]) -> AnnotationDiscoveryClient:
    """Return an AnnotationDiscoveryClient backed by a mock API."""
    api = MagicMock()
    api.list_cluster_custom_object.return_value = {"items": items}
    return AnnotationDiscoveryClient(api)


def _make_rule(pattern: str, destinations: dict) -> ScanRule:
    return ScanRule(pattern=pattern, compiled=re.compile(pattern), destinations=destinations)


# ----------------------------------------------------- list_application_rules --

class TestListApplicationRules:
    PREFIX = "pr-generator.io"

    def test_returns_rule_for_enabled_app(self):
        client = _make_client([
            _make_app("my-app", "default", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/pattern": "feature/.*",
                "pr-generator.io/destination.github": "main",
            })
        ])
        rules = client.list_application_rules(self.PREFIX)

        assert len(rules) == 1
        assert rules[0].pattern == "feature/.*"
        assert rules[0].destinations == {"github": "main"}
        assert rules[0].compiled.match("feature/my-branch")

    def test_skips_app_without_enabled_annotation(self):
        client = _make_client([
            _make_app("no-opt-in", "default", {
                "pr-generator.io/pattern": "feature/.*",
                "pr-generator.io/destination.github": "main",
            })
        ])
        assert client.list_application_rules(self.PREFIX) == []

    def test_skips_app_with_enabled_false(self):
        client = _make_client([
            _make_app("disabled", "default", {
                "pr-generator.io/enabled": "false",
                "pr-generator.io/pattern": "feature/.*",
                "pr-generator.io/destination.github": "main",
            })
        ])
        assert client.list_application_rules(self.PREFIX) == []

    def test_skips_app_without_pattern(self):
        client = _make_client([
            _make_app("no-pattern", "default", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/destination.github": "main",
            })
        ])
        assert client.list_application_rules(self.PREFIX) == []

    def test_skips_app_with_invalid_regex(self):
        client = _make_client([
            _make_app("bad-regex", "default", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/pattern": "(",
                "pr-generator.io/destination.github": "main",
            })
        ])
        assert client.list_application_rules(self.PREFIX) == []

    def test_skips_app_without_destinations(self):
        client = _make_client([
            _make_app("no-dest", "default", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/pattern": "feature/.*",
            })
        ])
        assert client.list_application_rules(self.PREFIX) == []

    def test_multiple_destinations(self):
        client = _make_client([
            _make_app("multi-dest", "argocd", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/pattern": "argocd-image-updater-.*",
                "pr-generator.io/destination.github": "develop",
                "pr-generator.io/destination.bitbucket": "dev",
            })
        ])
        rules = client.list_application_rules(self.PREFIX)

        assert len(rules) == 1
        assert rules[0].destinations == {"github": "develop", "bitbucket": "dev"}

    def test_custom_prefix(self):
        client = _make_client([
            _make_app("custom", "ns", {
                "my-tool.io/enabled": "true",
                "my-tool.io/pattern": "release/.*",
                "my-tool.io/destination.github": "main",
            })
        ])
        rules = client.list_application_rules("my-tool.io")

        assert len(rules) == 1
        assert rules[0].pattern == "release/.*"

    def test_returns_empty_on_k8s_api_error(self):
        api = MagicMock()
        api.list_cluster_custom_object.side_effect = Exception("connection refused")
        client = AnnotationDiscoveryClient(api)

        assert client.list_application_rules(self.PREFIX) == []

    def test_multiple_apps_only_enabled_ones_returned(self):
        client = _make_client([
            _make_app("enabled-app", "default", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/pattern": "feature/.*",
                "pr-generator.io/destination.github": "main",
            }),
            _make_app("disabled-app", "default", {
                "pr-generator.io/enabled": "false",
                "pr-generator.io/pattern": "feature/.*",
                "pr-generator.io/destination.github": "main",
            }),
            _make_app("no-opt-in-app", "default", {
                "pr-generator.io/pattern": "hotfix/.*",
                "pr-generator.io/destination.github": "main",
            }),
        ])
        rules = client.list_application_rules(self.PREFIX)

        assert len(rules) == 1
        assert rules[0].pattern == "feature/.*"

    def test_empty_items_list_returns_empty(self):
        api = MagicMock()
        api.list_cluster_custom_object.return_value = {"items": []}
        client = AnnotationDiscoveryClient(api)
        assert client.list_application_rules(self.PREFIX) == []

    def test_missing_items_key_returns_empty(self):
        api = MagicMock()
        api.list_cluster_custom_object.return_value = {}
        client = AnnotationDiscoveryClient(api)
        assert client.list_application_rules(self.PREFIX) == []

    def test_app_with_null_annotations_skipped(self):
        api = MagicMock()
        api.list_cluster_custom_object.return_value = {
            "items": [{"metadata": {"name": "nullann", "namespace": "ns", "annotations": None}}]
        }
        client = AnnotationDiscoveryClient(api)
        assert client.list_application_rules(self.PREFIX) == []

    def test_destination_with_empty_value_excluded(self):
        """Destinations with an empty-string value must be filtered out."""
        client = _make_client([
            _make_app("empty-val", "default", {
                "pr-generator.io/enabled": "true",
                "pr-generator.io/pattern": "feature/.*",
                "pr-generator.io/destination.github": "",       # empty — excluded
                "pr-generator.io/destination.bitbucket": "dev", # valid
            })
        ])
        rules = client.list_application_rules(self.PREFIX)
        assert len(rules) == 1
        assert "github" not in rules[0].destinations
        assert rules[0].destinations == {"bitbucket": "dev"}


class TestFromIncluster:
    def test_raises_runtime_error_on_missing_k8s_config(self, monkeypatch):
        """from_incluster() must raise RuntimeError when not inside a cluster."""
        import sys
        # Remove cached kubernetes module to force fresh import
        for mod in list(sys.modules):
            if mod.startswith("kubernetes"):
                del sys.modules[mod]

        # Inject a fake kubernetes module that raises on load_incluster_config
        fake_config = MagicMock()
        fake_config.load_incluster_config.side_effect = Exception("not in cluster")
        fake_k8s = MagicMock()
        fake_k8s.config = fake_config
        monkeypatch.setitem(sys.modules, "kubernetes", fake_k8s)
        monkeypatch.setitem(sys.modules, "kubernetes.config", fake_config)
        monkeypatch.setitem(sys.modules, "kubernetes.client", MagicMock())

        with pytest.raises(RuntimeError, match="in-cluster"):
            AnnotationDiscoveryClient.from_incluster()


# ----------------------------------------------------------- merge_rules --

class TestMergeRules:
    def test_annotation_only_pattern_is_additive(self):
        static = [_make_rule("feature/.*", {"github": "main"})]
        annotation = [_make_rule("hotfix/.*", {"github": "main"})]
        merged = merge_rules(static, annotation)
        patterns = {r.pattern for r in merged}
        assert patterns == {"feature/.*", "hotfix/.*"}

    def test_annotation_overrides_destination_on_collision(self):
        static = [_make_rule("feature/.*", {"github": "old-branch", "bitbucket": "dev"})]
        annotation = [_make_rule("feature/.*", {"github": "new-branch"})]
        merged = merge_rules(static, annotation)
        assert len(merged) == 1
        assert merged[0].destinations["github"] == "new-branch"
        assert merged[0].destinations["bitbucket"] == "dev"

    def test_static_only_destinations_preserved(self):
        static = [_make_rule("feature/.*", {"github": "main", "bitbucket": "dev"})]
        annotation = [_make_rule("feature/.*", {"github": "main"})]  # same value, no override
        merged = merge_rules(static, annotation)
        assert merged[0].destinations == {"github": "main", "bitbucket": "dev"}

    def test_empty_annotation_returns_static_unchanged(self):
        static = [_make_rule("feature/.*", {"github": "main"})]
        merged = merge_rules(static, [])
        assert merged == static

    def test_empty_static_returns_annotation(self):
        annotation = [_make_rule("feature/.*", {"github": "main"})]
        merged = merge_rules([], annotation)
        assert len(merged) == 1
        assert merged[0].pattern == "feature/.*"

    def test_both_empty_returns_empty(self):
        assert merge_rules([], []) == []

    def test_compiled_pattern_from_static_preserved_on_merge(self):
        """The compiled regex from the static rule is kept after merging."""
        static_rule = _make_rule("feature/.*", {"github": "old"})
        ann_rule = _make_rule("feature/.*", {"github": "new"})
        merged = merge_rules([static_rule], [ann_rule])
        assert merged[0].compiled is static_rule.compiled

    def test_multiple_annotation_rules_all_applied(self):
        static = [_make_rule("feature/.*", {"github": "main"})]
        annotation = [
            _make_rule("feature/.*", {"github": "develop"}),  # override
            _make_rule("release/.*", {"github": "main"}),     # additive
        ]
        merged = merge_rules(static, annotation)
        patterns = {r.pattern: r for r in merged}
        assert patterns["feature/.*"].destinations["github"] == "develop"
        assert "release/.*" in patterns
