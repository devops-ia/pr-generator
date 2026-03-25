"""Tests for config loading."""

import os
import re
import textwrap
import pytest


def _write_config(tmp_path, content: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(content))
    return str(path)


_FAKE_PEM = "-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----"


class TestLoadFromFile:
    def test_single_rule_both_providers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITBUCKET_TOKEN", "bb-token")
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM)
        path = _write_config(tmp_path, """
            scan_frequency: 60
            log_level: DEBUG
            dry_run: true
            health_port: 9090
            providers:
              github:
                enabled: true
                owner: my-org
                repo: my-repo
                app_id: "111"
                installation_id: "222"
                private_key_path: /nonexistent
                timeout: 10
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
                timeout: 15
            rules:
              - pattern: "feature/.*"
                destinations:
                  github: main
                  bitbucket: develop
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()

        assert cfg.scan_frequency == 60
        assert cfg.log_level == "DEBUG"
        assert cfg.dry_run is True
        assert cfg.health_port == 9090
        assert "github" in cfg.providers
        assert "bitbucket" in cfg.providers
        assert cfg.providers["bitbucket"].token == "bb-token"
        assert len(cfg.rules) == 1
        assert cfg.rules[0].pattern == "feature/.*"
        assert cfg.rules[0].destinations == {"github": "main", "bitbucket": "develop"}
        assert cfg.rules[0].compiled.match("feature/my-branch")

    def test_multiple_rules(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: ".*-nonpro-.*"
                destinations:
                  bitbucket: nonpro
              - pattern: ".*-pro-.*"
                destinations:
                  bitbucket: master
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert len(cfg.rules) == 2
        assert cfg.rules[0].destinations == {"bitbucket": "nonpro"}
        assert cfg.rules[1].destinations == {"bitbucket": "master"}

    def test_missing_rules_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BB_TOKEN_TEST", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: w
                repo_slug: r
                token_env: BB_TOKEN_TEST
            rules: []
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="no rules"):
            load_config()

    def test_invalid_regex_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: "("
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="Invalid regex"):
            load_config()

    def test_missing_github_private_key_raises(self, tmp_path, monkeypatch):
        """GitHub App provider with no private key should fail at load time."""
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: true
                owner: org
                repo: repo
                app_id: "1"
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="no private key"):
            load_config()

    def test_missing_bitbucket_token_raises(self, tmp_path, monkeypatch):
        """Bitbucket provider with empty token env var should fail at load time."""
        monkeypatch.delenv("BB_MISSING_TOKEN", raising=False)
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BB_MISSING_TOKEN
            rules:
              - pattern: ".*"
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="BB_MISSING_TOKEN"):
            load_config()


class TestGitHubPATConfig:
    def test_pat_auth_method_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent("""
            providers:
              github:
                enabled: true
                auth_method: pat
                owner: my-org
                repo: my-repo
                token_env: GITHUB_TOKEN
                timeout: 10
            rules:
              - pattern: "feature/.*"
                destinations:
                  github: main
        """))
        monkeypatch.setenv("CONFIG_PATH", str(path))
        from pr_generator.config import load_config
        cfg = load_config()
        gh = cfg.providers["github"]
        assert gh.auth_method == "pat"
        assert gh.token == "ghp_testtoken"
        assert gh.app_id == ""
        assert gh.private_key == ""

    def test_pat_custom_token_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_GH_TOKEN", "ghp_custom")
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent("""
            providers:
              github:
                enabled: true
                auth_method: pat
                owner: org
                repo: repo
                token_env: MY_GH_TOKEN
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """))
        monkeypatch.setenv("CONFIG_PATH", str(path))
        from pr_generator.config import load_config
        cfg = load_config()
        assert cfg.providers["github"].token == "ghp_custom"

    def test_app_auth_method_default(self, tmp_path, monkeypatch):
        """auth_method defaults to 'app' when not specified."""
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM)
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent("""
            providers:
              github:
                enabled: true
                owner: org
                repo: repo
                app_id: "111"
                installation_id: "222"
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """))
        monkeypatch.setenv("CONFIG_PATH", str(path))
        from pr_generator.config import load_config
        cfg = load_config()
        assert cfg.providers["github"].auth_method == "app"

    def test_log_format_json_from_yaml(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent("""
            log_format: json
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: ".*"
                destinations:
                  bitbucket: main
        """))
        monkeypatch.setenv("CONFIG_PATH", str(path))
        from pr_generator.config import load_config
        assert load_config().log_format == "json"


class TestMultiOrgGitHub:
    """Tests for multiple GitHub provider instances (different orgs/repos)."""

    def test_two_github_providers_different_orgs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN_ACME", "ghp_acme")
        monkeypatch.setenv("GITHUB_TOKEN_SKW", "ghp_skw")
        path = _write_config(tmp_path, """
            providers:
              github-acme:
                type: github
                enabled: true
                auth_method: pat
                owner: acme-org
                repo: backend
                token_env: GITHUB_TOKEN_ACME
              github-skunkworks:
                type: github
                enabled: true
                auth_method: pat
                owner: skunkworks-org
                repo: platform
                token_env: GITHUB_TOKEN_SKW
            rules:
              - pattern: "feature/.*"
                destinations:
                  github-acme: main
                  github-skunkworks: develop
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()

        assert set(cfg.providers.keys()) == {"github-acme", "github-skunkworks"}
        acme = cfg.providers["github-acme"]
        assert acme.type == "github"
        assert acme.owner == "acme-org"
        assert acme.repo == "backend"
        assert acme.token == "ghp_acme"
        skw = cfg.providers["github-skunkworks"]
        assert skw.type == "github"
        assert skw.owner == "skunkworks-org"
        assert skw.token == "ghp_skw"
        assert cfg.rules[0].destinations == {
            "github-acme": "main",
            "github-skunkworks": "develop",
        }

    def test_named_provider_defaults_type_from_key(self, tmp_path, monkeypatch):
        """Key 'github' without explicit type should still work (backward compat)."""
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM)
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: true
                owner: org
                repo: repo
                app_id: "1"
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert cfg.providers["github"].type == "github"

    def test_named_provider_unknown_type_raises(self, tmp_path, monkeypatch):
        """A named provider with an unrecognised type should raise ValueError."""
        path = _write_config(tmp_path, """
            providers:
              my-provider:
                type: gitlab
                enabled: true
                owner: org
                repo: repo
            rules:
              - pattern: ".*"
                destinations:
                  my-provider: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="unknown or missing type"):
            load_config()

    def test_named_provider_missing_type_raises(self, tmp_path, monkeypatch):
        """A non-standard provider key without 'type' should raise ValueError."""
        path = _write_config(tmp_path, """
            providers:
              my-github-instance:
                enabled: true
                owner: org
                repo: repo
            rules:
              - pattern: ".*"
                destinations:
                  my-github-instance: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="unknown or missing type"):
            load_config()

    def test_mixed_github_and_bitbucket_named_providers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN_ORG", "ghp_org")
        monkeypatch.setenv("BB_TOKEN", "bb_tok")
        path = _write_config(tmp_path, """
            providers:
              github-myorg:
                type: github
                enabled: true
                auth_method: pat
                owner: my-org
                repo: app
                token_env: GITHUB_TOKEN_ORG
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BB_TOKEN
            rules:
              - pattern: "feature/.*"
                destinations:
                  github-myorg: main
                  bitbucket: develop
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert cfg.providers["github-myorg"].type == "github"
        assert cfg.providers["bitbucket"].type == "bitbucket"


class TestConfigValidationEdgeCases:
    """Cover validation branches not exercised by the main test classes."""

    def test_no_enabled_providers_raises(self, tmp_path, monkeypatch):
        """All providers disabled → ValueError about no enabled providers."""
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: false
                owner: org
                repo: repo
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="no enabled providers"):
            load_config()

    def test_non_dict_provider_entry_skipped(self, tmp_path, monkeypatch):
        """A provider entry that isn't a dict is silently skipped."""
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bad_entry: "not-a-dict"
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: ".*"
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert "bitbucket" in cfg.providers
        assert "bad_entry" not in cfg.providers

    def test_disabled_provider_not_loaded(self, tmp_path, monkeypatch):
        """A provider with enabled: false is excluded from the result."""
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
              github:
                enabled: false
                owner: org
                repo: repo
            rules:
              - pattern: ".*"
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert "github" not in cfg.providers

    def test_github_missing_owner_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM)
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: true
                repo: repo
                app_id: "1"
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="'owner' and 'repo' are required"):
            load_config()

    def test_github_missing_app_id_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _FAKE_PEM)
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: true
                owner: org
                repo: repo
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="'app_id' is required"):
            load_config()

    def test_github_pat_missing_token_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_GH_PAT", raising=False)
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: true
                auth_method: pat
                owner: org
                repo: repo
                token_env: MISSING_GH_PAT
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="MISSING_GH_PAT"):
            load_config()

    def test_bitbucket_missing_workspace_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: ".*"
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="'workspace' and 'repo_slug' are required"):
            load_config()

    def test_private_key_loaded_from_file(self, tmp_path, monkeypatch):
        """private_key_path pointing to an existing file loads the key from disk."""
        key_file = tmp_path / "app.pem"
        key_file.write_text(_FAKE_PEM)
        monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
        path = _write_config(tmp_path, f"""
            providers:
              github:
                enabled: true
                owner: org
                repo: repo
                app_id: "1"
                private_key_path: {key_file}
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert cfg.providers["github"].private_key == _FAKE_PEM

    def test_private_key_base64_decoded_from_env(self, tmp_path, monkeypatch):
        """GITHUB_APP_PRIVATE_KEY as base64 is decoded automatically."""
        import base64
        encoded = base64.b64encode(_FAKE_PEM.encode()).decode()
        monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", encoded)
        path = _write_config(tmp_path, """
            providers:
              github:
                enabled: true
                owner: org
                repo: repo
                app_id: "1"
            rules:
              - pattern: ".*"
                destinations:
                  github: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert cfg.providers["github"].private_key == _FAKE_PEM

    def test_rule_with_empty_pattern_skipped(self, tmp_path, monkeypatch):
        """A rule with no pattern is silently skipped."""
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: ""
                destinations:
                  bitbucket: main
              - pattern: "feature/.*"
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert len(cfg.rules) == 1
        assert cfg.rules[0].pattern == "feature/.*"

    def test_rule_with_no_destinations_skipped(self, tmp_path, monkeypatch):
        """A rule with empty destinations is silently skipped."""
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: "feature/.*"
                destinations: {}
              - pattern: "release/.*"
                destinations:
                  bitbucket: main
        """)
        monkeypatch.setenv("CONFIG_PATH", path)
        from pr_generator.config import load_config
        cfg = load_config()
        assert len(cfg.rules) == 1
        assert cfg.rules[0].pattern == "release/.*"


class TestNullYamlValues:
    """Regression tests for null/empty YAML values that previously caused AttributeError."""

    def _base_config(self, tmp_path, monkeypatch, content: str) -> str:
        monkeypatch.setenv("BITBUCKET_TOKEN", "tok")
        path = _write_config(tmp_path, content)
        monkeypatch.setenv("CONFIG_PATH", path)
        return path

    def test_empty_yaml_file_raises(self, tmp_path, monkeypatch):
        """An empty YAML file must raise ValueError, not AttributeError."""
        path = tmp_path / "config.yaml"
        path.write_text("")
        monkeypatch.setenv("CONFIG_PATH", str(path))
        from pr_generator.config import load_config
        with pytest.raises(ValueError):
            load_config()

    def test_null_providers_section_raises(self, tmp_path, monkeypatch):
        """providers: with no value (null) must raise ValueError, not AttributeError."""
        self._base_config(tmp_path, monkeypatch, """
            providers:
            rules:
              - pattern: "feature/.*"
                destinations:
                  bitbucket: main
        """)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="no enabled providers"):
            load_config()

    def test_null_rules_section_raises(self, tmp_path, monkeypatch):
        """rules: with no value (null) must raise ValueError, not AttributeError."""
        self._base_config(tmp_path, monkeypatch, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
        """)
        from pr_generator.config import load_config
        with pytest.raises(ValueError, match="no rules"):
            load_config()

    def test_null_destinations_in_rule_skipped(self, tmp_path, monkeypatch):
        """destinations: with no value (null) must be treated as empty and skipped."""
        self._base_config(tmp_path, monkeypatch, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              - pattern: "feature/.*"
                destinations:
              - pattern: "release/.*"
                destinations:
                  bitbucket: main
        """)
        from pr_generator.config import load_config
        cfg = load_config()
        assert len(cfg.rules) == 1
        assert cfg.rules[0].pattern == "release/.*"

    def test_null_rule_item_skipped(self, tmp_path, monkeypatch):
        """A null entry in the rules list must be skipped, not crash."""
        self._base_config(tmp_path, monkeypatch, """
            providers:
              bitbucket:
                enabled: true
                workspace: ws
                repo_slug: rs
                token_env: BITBUCKET_TOKEN
            rules:
              -
              - pattern: "release/.*"
                destinations:
                  bitbucket: main
        """)
        from pr_generator.config import load_config
        cfg = load_config()
        assert len(cfg.rules) == 1
        assert cfg.rules[0].pattern == "release/.*"

    def test_config_file_not_found_raises(self, monkeypatch):
        """Missing config file must raise FileNotFoundError."""
        monkeypatch.setenv("CONFIG_PATH", "/nonexistent/path/config.yaml")
        from pr_generator.config import load_config
        with pytest.raises(FileNotFoundError):
            load_config()

