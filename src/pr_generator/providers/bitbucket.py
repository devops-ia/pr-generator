"""Bitbucket Cloud provider implementation."""

from __future__ import annotations

import logging
from typing import Any

from pr_generator.http_client import request_with_retry
from pr_generator.models import ProviderConfig


class BitbucketError(Exception):
    """Raised when a Bitbucket API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BitbucketProvider:
    """Bitbucket Cloud provider.

    Receives all configuration via constructor — no module-level env-var reads.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._name = config.name
        self._workspace = config.workspace
        self._repo_slug = config.repo_slug
        self._token = config.token
        self._timeout = config.timeout
        self._close_source_branch = config.close_source_branch
        self._api_url = (
            f"https://api.bitbucket.org/2.0/repositories"
            f"/{self._workspace}/{self._repo_slug}"
        )
        self._logger = logging.getLogger("pr_generator.providers.bitbucket")

        # Per-cycle cache (reset via reset_cycle_cache)
        self._pr_cache: dict[tuple[str, str], bool] = {}

    # ------------------------------------------------------------------
    # ProviderInterface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    def get_branches(self) -> list[str]:
        """Fetch all branch names (handles pagination)."""
        self._logger.info("[%s] Step: get_branches action=start", self._name)
        if not (self._token and self._workspace and self._repo_slug):
            self._logger.error("[%s] Step: get_branches action=error detail=missing configuration", self._name)
            return []

        url = f"{self._api_url}/refs/branches"
        names: list[str] = []
        page = 1

        while True:
            self._logger.debug("[%s] Step: get_branches action=fetch page=%d", self._name, page)
            resp = self._request("GET", url, params={"pagelen": 100, "page": page})
            data = resp.json()
            page_values: list[dict[str, Any]] = data.get("values", [])
            names.extend(b["name"] for b in page_values if b.get("name"))
            self._logger.debug(
                "[%s] Step: get_branches action=fetch page=%d count=%d total=%d",
                self._name, page, len(page_values), len(names),
            )
            if "next" in data:
                page += 1
            else:
                break

        self._logger.info("[%s] Step: get_branches action=end total=%d", self._name, len(names))
        return names

    def check_existing_pr(self, source: str, destination: str) -> bool:
        """Return True if an open PR from source to destination already exists."""
        self._logger.info(
            "[%s] Step: check_existing_pr action=start source=%s dest=%s",
            self._name, source, destination,
        )
        key = (source, destination)
        if key in self._pr_cache:
            self._logger.debug("[%s] Step: check_existing_pr action=cache_hit source=%s dest=%s", self._name, source, destination)
            return self._pr_cache[key]

        resp = self._request(
            "GET",
            f"{self._api_url}/pullrequests",
            params={
                "state": "OPEN",
                "q": f'source.branch.name="{source}" AND destination.branch.name="{destination}"',
                "pagelen": 1,
            },
        )
        exists = len(resp.json().get("values", [])) > 0
        self._pr_cache[key] = exists
        self._logger.info(
            "[%s] Step: check_existing_pr action=end source=%s dest=%s exists=%s",
            self._name, source, destination, str(exists).lower(),
        )
        return exists

    def create_pull_request(self, source: str, destination: str) -> None:
        """Create a PR from source to destination including default reviewers."""
        reviewers = self._get_default_reviewers()
        self._logger.info(
            "[%s] Step: create_pull_request action=start source=%s dest=%s reviewers=%d",
            self._name, source, destination, len(reviewers),
        )
        payload = {
            "title": f"Merge {source} into {destination}",
            "source": {"branch": {"name": source}},
            "destination": {"branch": {"name": destination}},
            "reviewers": reviewers,
            "close_source_branch": self._close_source_branch,
        }
        resp = self._request("POST", f"{self._api_url}/pullrequests", json=payload)
        self._pr_cache[(source, destination)] = True
        self._logger.info(
            "[%s] Step: create_pull_request action=end source=%s dest=%s status=created",
            self._name, source, destination,
        )

    def reset_cycle_cache(self) -> None:
        """Clear per-cycle PR-existence cache."""
        self._pr_cache.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_default_reviewers(self) -> list[dict[str, Any]]:
        self._logger.info("[%s] Step: get_default_reviewers action=start", self._name)
        if not (self._token and self._workspace and self._repo_slug):
            self._logger.warning("[%s] Step: get_default_reviewers action=skip detail=missing config", self._name)
            return []
        url = f"{self._api_url}/default-reviewers"
        resp = self._request("GET", url)
        reviewers = [{"uuid": r.get("uuid")} for r in resp.json().get("values", [])]
        self._logger.info("[%s] Step: get_default_reviewers action=end count=%d", self._name, len(reviewers))
        return reviewers

    def _request(self, method: str, url: str, **kwargs):
        return request_with_retry(
            logger=self._logger,
            client_name=self._name,
            method=method,
            url=url,
            timeout=self._timeout,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            exception_cls=BitbucketError,
            should_retry=self._should_retry,
            **kwargs,
        )

    def _should_retry(self, status_code: int | None, exc: Exception | None) -> bool:
        if exc is not None:
            self._logger.warning("[%s] Retry due to request failure: %s", self._name, exc)
            return True
        return bool(status_code and (500 <= status_code < 600 or status_code in (408, 429)))
