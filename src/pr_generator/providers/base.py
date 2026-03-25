"""Provider interface contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProviderInterface(Protocol):
    """Contract that every Git provider must fulfil."""

    @property
    def name(self) -> str:
        """Lowercase provider identifier: 'github' or 'bitbucket'."""
        ...  # pragma: no cover

    def get_branches(self) -> list[str]:
        """Return all branch names in the repository (handles pagination).

        Raises a provider-specific exception on API failure.
        """
        ...  # pragma: no cover

    def check_existing_pr(self, source: str, destination: str) -> bool:
        """Return True if an open PR from source to destination already exists."""
        ...  # pragma: no cover

    def create_pull_request(self, source: str, destination: str) -> None:
        """Create a PR from source to destination.

        Raises a provider-specific exception on API failure.
        """
        ...  # pragma: no cover

    def reset_cycle_cache(self) -> None:
        """Clear any per-cycle caches. No-op if the provider has no cache."""
        ...  # pragma: no cover
