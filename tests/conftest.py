"""Shared fixtures for the test suite."""

import pytest


@pytest.fixture
def github_app_config():
    from pr_generator.models import ProviderConfig
    return ProviderConfig(
        name="github",
        type="github",
        enabled=True,
        owner="test-owner",
        repo="test-repo",
        auth_method="app",
        app_id="12345",
        installation_id="67890",
        private_key="fake-pem",
        timeout=5.0,
    )


@pytest.fixture
def github_pat_config():
    from pr_generator.models import ProviderConfig
    return ProviderConfig(
        name="github",
        type="github",
        enabled=True,
        owner="test-owner",
        repo="test-repo",
        auth_method="pat",
        token="ghp_testtoken123",
        timeout=5.0,
    )


@pytest.fixture
def bitbucket_provider_config():
    from pr_generator.models import ProviderConfig
    return ProviderConfig(
        name="bitbucket",
        type="bitbucket",
        enabled=True,
        workspace="test-workspace",
        repo_slug="test-repo",
        token="test-token",
        timeout=5.0,
    )
