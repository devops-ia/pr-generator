"""Unit tests for GitHub and Bitbucket provider implementations."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from pr_generator.providers.bitbucket import BitbucketError, BitbucketProvider
from pr_generator.providers.github import GitHubError, GitHubProvider


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _mock_response(status_code: int = 200, json_data=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data if json_data is not None else {}
    return resp


# ──────────────────────────────────────────────────────────
# GitHub App auth — token caching and JWT logic
# ──────────────────────────────────────────────────────────

class TestGitHubAppAuth:
    """Tests for GitHub App JWT and installation token caching."""

    @pytest.fixture
    def provider(self, github_app_config):
        return GitHubProvider(github_app_config)

    def test_get_jwt_raises_without_credentials(self, provider):
        provider._app_id = ""
        with pytest.raises(RuntimeError, match="Missing GITHUB_APP_ID"):
            provider._new_jwt()

    def test_get_jwt_cached_within_window(self, provider):
        provider._jwt_cache = "cached-jwt"
        provider._jwt_exp = time.time() + 300  # well within expiry

        with patch.object(provider, "_new_jwt") as mock_new_jwt:
            result = provider._get_jwt()

        mock_new_jwt.assert_not_called()
        assert result == "cached-jwt"

    def test_get_jwt_refreshed_when_expired(self, provider):
        provider._jwt_cache = "old-jwt"
        provider._jwt_exp = time.time() - 1  # already expired

        with patch.object(provider, "_new_jwt", return_value="new-jwt"):
            result = provider._get_jwt()

        assert result == "new-jwt"

    def test_get_installation_token_cached(self, provider):
        provider._install_token = "cached-token"
        provider._install_token_exp = time.time() + 300

        with patch.object(provider, "_request") as mock_req:
            result = provider._get_installation_token()

        mock_req.assert_not_called()
        assert result == "cached-token"

    def test_get_installation_token_fetched_when_missing(self, provider):
        install_resp = _mock_response(201, {
            "token": "ghs_fresh_token",
            "expires_at": "2099-01-01T00:00:00Z",
        })
        with patch.object(provider, "_request", return_value=install_resp):
            result = provider._get_installation_token()

        assert result == "ghs_fresh_token"
        assert provider._install_token == "ghs_fresh_token"

    def test_get_installation_token_uses_55min_default_on_bad_expiry(self, provider):
        install_resp = _mock_response(201, {"token": "ghs_tok", "expires_at": "not-a-date"})
        before = time.time()
        with patch.object(provider, "_request", return_value=install_resp):
            provider._get_installation_token()
        after = time.time()

        # 55 min default: expiry should be ~3300 seconds from now
        assert 3290 < provider._install_token_exp - before < 3310 + (after - before)

    def test_resolve_installation_id_uses_config_value(self, provider):
        """When installation_id is provided in config, no API call is made."""
        assert provider._installation_id == "67890"
        with patch.object(provider, "_request") as mock_req:
            result = provider._resolve_installation_id()
        mock_req.assert_not_called()
        assert result == "67890"

    def test_resolve_installation_id_fetches_and_caches_when_missing(self, provider):
        """When installation_id is absent, it is fetched from the API and cached."""
        provider._installation_id = ""
        api_resp = _mock_response(200, {"id": 99999})

        with patch.object(provider, "_request", return_value=api_resp) as mock_req:
            result1 = provider._resolve_installation_id()
            # Second call should use cached value — no extra API call
            result2 = provider._resolve_installation_id()

        assert result1 == "99999"
        assert result2 == "99999"
        assert provider._installation_id == "99999"  # cached on instance
        assert mock_req.call_count == 1  # only one API call total

    def test_resolve_installation_id_raises_when_api_returns_no_id(self, provider):
        provider._installation_id = ""
        with patch.object(provider, "_request", return_value=_mock_response(200, {})):
            with pytest.raises(RuntimeError, match="Could not resolve installation id"):
                provider._resolve_installation_id()

    def test_headers_use_installation_token_for_app_auth(self, provider):
        with patch.object(provider, "_get_installation_token", return_value="ghs_tok"):
            hdrs = provider._headers(installation=True)
        assert hdrs["Authorization"] == "Bearer ghs_tok"

    def test_headers_use_jwt_for_non_installation_calls(self, provider):
        with patch.object(provider, "_get_jwt", return_value="jwt.token.here"):
            hdrs = provider._headers(installation=False)
        assert hdrs["Authorization"] == "Bearer jwt.token.here"

    def test_new_jwt_generates_token_with_valid_credentials(self, provider):
        with patch("pr_generator.providers.github.jwt.encode", return_value="signed.jwt") as mock_enc:
            result = provider._new_jwt()
        assert result == "signed.jwt"
        call_payload = mock_enc.call_args[0][0]
        assert call_payload["iss"] == "12345"
        assert "iat" in call_payload and "exp" in call_payload

    def test_get_branches_returns_empty_when_app_config_incomplete(self):
        from pr_generator.models import ProviderConfig
        cfg = ProviderConfig(
            name="github", type="github", enabled=True,
            auth_method="app", owner="org", repo="repo",
            app_id="", private_key="",  # missing credentials
        )
        prov = GitHubProvider(cfg)
        assert prov.get_branches() == []


# ──────────────────────────────────────────────────────────
# GitHub PAT provider (simpler — no token caching)
# ──────────────────────────────────────────────────────────

class TestGitHubProviderPAT:
    """Tests for GitHub provider using PAT authentication."""

    @pytest.fixture
    def provider(self, github_pat_config):
        return GitHubProvider(github_pat_config)

    def test_name_matches_config(self, provider):
        assert provider.name == "github"

    def test_get_branches_single_page(self, provider):
        page_data = [{"name": "main"}, {"name": "feature/x"}]
        with patch.object(provider, "_request", return_value=_mock_response(200, page_data)):
            branches = provider.get_branches()
        assert branches == ["main", "feature/x"]

    def test_get_branches_empty(self, provider):
        with patch.object(provider, "_request", return_value=_mock_response(200, [])):
            branches = provider.get_branches()
        assert branches == []

    def test_get_branches_multi_page(self, provider):
        """Two pages: first returns 100 items (triggers next page), second returns 2."""
        page1 = [{"name": f"branch-{i}"} for i in range(100)]
        page2 = [{"name": "extra-1"}, {"name": "extra-2"}]
        responses = iter([_mock_response(200, page1), _mock_response(200, page2)])
        with patch.object(provider, "_request", side_effect=lambda *a, **kw: next(responses)):
            branches = provider.get_branches()
        assert len(branches) == 102

    def test_check_existing_pr_found(self, provider):
        pr_list = [{"number": 1, "title": "Merge feature/x into main"}]
        with patch.object(provider, "_request", return_value=_mock_response(200, pr_list)):
            assert provider.check_existing_pr("feature/x", "main") is True

    def test_check_existing_pr_not_found(self, provider):
        with patch.object(provider, "_request", return_value=_mock_response(200, [])):
            assert provider.check_existing_pr("feature/x", "main") is False

    def test_check_existing_pr_uses_cache(self, provider):
        """Second call with same args should not make an HTTP request."""
        with patch.object(provider, "_request", return_value=_mock_response(200, [])) as mock_req:
            provider.check_existing_pr("feature/x", "main")
            provider.check_existing_pr("feature/x", "main")
        assert mock_req.call_count == 1

    def test_reset_cycle_cache_clears_pr_cache(self, provider):
        with patch.object(provider, "_request", return_value=_mock_response(200, [])) as mock_req:
            provider.check_existing_pr("feature/x", "main")
            provider.reset_cycle_cache()
            provider.check_existing_pr("feature/x", "main")
        assert mock_req.call_count == 2

    def test_create_pull_request_success(self, provider):
        pr_resp = {"number": 42, "title": "Merge feature/x into main"}
        with patch.object(provider, "_branch_exists", return_value=True), \
             patch.object(provider, "_request", return_value=_mock_response(201, pr_resp)):
            provider.create_pull_request("feature/x", "main")
        assert provider._pr_cache[("feature/x", "main")] is True

    def test_create_pull_request_skips_missing_branch(self, provider):
        with patch.object(provider, "_branch_exists", return_value=False), \
             patch.object(provider, "_request") as mock_req:
            provider.create_pull_request("feature/gone", "main")
        mock_req.assert_not_called()

    def test_branch_exists_returns_true(self, provider):
        with patch.object(provider, "_request", return_value=_mock_response(200, {"name": "feature/x"})):
            assert provider._branch_exists("feature/x") is True

    def test_branch_exists_returns_false_on_404(self, provider):
        err = GitHubError("GitHub API error 404: not found", status_code=404)
        with patch.object(provider, "_request", side_effect=err):
            assert provider._branch_exists("feature/gone") is False

    def test_branch_exists_reraises_non_404(self, provider):
        err = GitHubError("GitHub API error 500: server error", status_code=500)
        with patch.object(provider, "_request", side_effect=err):
            with pytest.raises(GitHubError):
                provider._branch_exists("feature/x")

    def test_headers_use_pat(self, provider):
        hdrs = provider._headers()
        assert hdrs["Authorization"] == "token ghp_testtoken123"

    def test_get_branches_returns_empty_when_config_incomplete(self, github_pat_config):
        from pr_generator.models import ProviderConfig
        cfg = ProviderConfig(
            name="github", type="github", enabled=True,
            auth_method="pat", owner="", repo="", token="",
        )
        prov = GitHubProvider(cfg)
        assert prov.get_branches() == []

    def test_should_retry_true_on_exception(self, provider):
        assert provider._should_retry(None, RuntimeError("conn error")) is True

    def test_should_retry_true_on_5xx(self, provider):
        assert provider._should_retry(503, None) is True

    def test_should_retry_true_on_429(self, provider):
        assert provider._should_retry(429, None) is True

    def test_should_retry_false_on_4xx(self, provider):
        assert provider._should_retry(404, None) is False

    def test_branch_exists_uses_cache(self, provider):
        provider._branch_cache["feature/cached"] = True
        with patch.object(provider, "_request") as mock_req:
            result = provider._branch_exists("feature/cached")
        mock_req.assert_not_called()
        assert result is True

    def test_request_delegates_to_retry_client(self, provider):
        """_request must call request_with_retry (exercises the method body)."""
        with patch("pr_generator.providers.github.request_with_retry", return_value=_mock_response(200)) as mock_retry:
            provider._request("GET", "https://api.github.com/repos/org/repo/branches")
        mock_retry.assert_called_once()
        call_kw = mock_retry.call_args.kwargs
        assert call_kw["method"] == "GET"
        assert call_kw["exception_cls"] is GitHubError


# ──────────────────────────────────────────────────────────
# Bitbucket provider
# ──────────────────────────────────────────────────────────

class TestBitbucketProvider:
    """Tests for Bitbucket Cloud provider."""

    @pytest.fixture
    def provider(self, bitbucket_provider_config):
        return BitbucketProvider(bitbucket_provider_config)

    def test_name_matches_config(self, provider):
        assert provider.name == "bitbucket"

    def test_get_branches_single_page(self, provider):
        data = {"values": [{"name": "main"}, {"name": "feature/y"}]}
        with patch.object(provider, "_request", return_value=_mock_response(200, data)):
            branches = provider.get_branches()
        assert branches == ["main", "feature/y"]

    def test_get_branches_multi_page(self, provider):
        """Uses 'next' key to determine pagination."""
        page1 = {"values": [{"name": "a"}, {"name": "b"}], "next": "http://page2"}
        page2 = {"values": [{"name": "c"}]}
        responses = iter([_mock_response(200, page1), _mock_response(200, page2)])
        with patch.object(provider, "_request", side_effect=lambda *a, **kw: next(responses)):
            branches = provider.get_branches()
        assert branches == ["a", "b", "c"]

    def test_get_branches_missing_token_returns_empty(self, bitbucket_provider_config):
        from pr_generator.models import ProviderConfig
        cfg = ProviderConfig(
            name="bitbucket", type="bitbucket", enabled=True,
            workspace="ws", repo_slug="rs", token="",
        )
        prov = BitbucketProvider(cfg)
        assert prov.get_branches() == []

    def test_check_existing_pr_found(self, provider):
        data = {"values": [{"id": 1}]}
        with patch.object(provider, "_request", return_value=_mock_response(200, data)):
            assert provider.check_existing_pr("feature/y", "main") is True

    def test_check_existing_pr_not_found(self, provider):
        data = {"values": []}
        with patch.object(provider, "_request", return_value=_mock_response(200, data)):
            assert provider.check_existing_pr("feature/y", "main") is False

    def test_check_existing_pr_uses_query_filter(self, provider):
        """Verify the q param is sent (efficient single-request lookup)."""
        data = {"values": []}
        with patch.object(provider, "_request", return_value=_mock_response(200, data)) as mock_req:
            provider.check_existing_pr("feature/y", "main")
        call_kwargs = mock_req.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert "q" in params
        assert 'source.branch.name="feature/y"' in params["q"]
        assert 'destination.branch.name="main"' in params["q"]
        assert params.get("pagelen") == 1

    def test_check_existing_pr_uses_cache(self, provider):
        data = {"values": []}
        with patch.object(provider, "_request", return_value=_mock_response(200, data)) as mock_req:
            provider.check_existing_pr("feature/y", "main")
            provider.check_existing_pr("feature/y", "main")
        assert mock_req.call_count == 1

    def test_reset_cycle_cache_clears_pr_cache(self, provider):
        data = {"values": []}
        with patch.object(provider, "_request", return_value=_mock_response(200, data)) as mock_req:
            provider.check_existing_pr("feature/y", "main")
            provider.reset_cycle_cache()
            provider.check_existing_pr("feature/y", "main")
        assert mock_req.call_count == 2

    def test_create_pull_request_success(self, provider):
        reviewers_data = {"values": [{"uuid": "{abc-123}"}]}
        pr_data = {"id": 10, "title": "Merge feature/y into main"}
        responses = iter([
            _mock_response(200, reviewers_data),
            _mock_response(201, pr_data),
        ])
        with patch.object(provider, "_request", side_effect=lambda *a, **kw: next(responses)):
            provider.create_pull_request("feature/y", "main")
        assert provider._pr_cache[("feature/y", "main")] is True

    def test_create_pull_request_includes_close_source_branch(self, provider):
        """close_source_branch from config must appear in the POST payload."""
        reviewers_data = {"values": []}
        pr_data = {"id": 11}
        responses = iter([_mock_response(200, reviewers_data), _mock_response(201, pr_data)])
        with patch.object(provider, "_request", side_effect=lambda *a, **kw: next(responses)) as mock_req:
            provider.create_pull_request("feature/y", "main")
        pr_call = list(mock_req.call_args_list)[-1]
        payload = pr_call.kwargs.get("json", {})
        assert "close_source_branch" in payload
        assert payload["close_source_branch"] is True

    def test_bitbucket_error_carries_status_code(self):
        """BitbucketError.__init__ must store the status_code attribute."""
        err = BitbucketError("boom", status_code=503)
        assert str(err) == "boom"
        assert err.status_code == 503

    def test_bitbucket_error_defaults_status_code_to_none(self):
        err = BitbucketError("network error")
        assert err.status_code is None

    def test_should_retry_returns_true_on_exception(self, provider):
        assert provider._should_retry(None, exc=ValueError("timeout")) is True

    def test_should_retry_returns_true_on_5xx(self, provider):
        assert provider._should_retry(503, exc=None) is True

    def test_should_retry_returns_false_on_4xx(self, provider):
        assert provider._should_retry(404, exc=None) is False

    def test_get_default_reviewers_returns_empty_on_missing_config(self):
        from pr_generator.models import ProviderConfig
        cfg = ProviderConfig(
            name="bitbucket", type="bitbucket", enabled=True,
            token="", workspace="", repo_slug="",
        )
        prov = BitbucketProvider(cfg)
        result = prov._get_default_reviewers()
        assert result == []

    def test_request_delegates_to_retry_client(self, provider):
        """_request must call request_with_retry (exercises the method body)."""
        with patch("pr_generator.providers.bitbucket.request_with_retry", return_value=_mock_response(200)) as mock_retry:
            provider._request("GET", "https://api.bitbucket.org/2.0/repos/ws/r")
        mock_retry.assert_called_once()
        call_kw = mock_retry.call_args.kwargs
        assert call_kw["method"] == "GET"
        assert call_kw["exception_cls"] is BitbucketError
