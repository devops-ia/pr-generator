"""GitHub App provider implementation."""

from __future__ import annotations

import logging
import time
from datetime import datetime

import jwt

from pr_generator.http_client import request_with_retry
from pr_generator.models import ProviderConfig

_API_BASE = "https://api.github.com"


class GitHubError(Exception):
    """Raised when a GitHub API call fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubProvider:
    """GitHub App provider.

    Receives all configuration via constructor — no module-level env-var reads.
    JWT and installation tokens are cached within the instance and refreshed
    automatically before expiry.
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._name = config.name
        self._owner = config.owner
        self._repo = config.repo
        self._auth_method = config.auth_method  # "app" | "pat"
        self._pat = config.token                # used when auth_method == "pat"
        self._app_id = config.app_id
        self._installation_id = config.installation_id
        self._private_key = config.private_key
        self._timeout = config.timeout
        self._repo_root = f"{_API_BASE}/repos/{self._owner}/{self._repo}"
        self._logger = logging.getLogger("pr_generator.providers.github")

        # Token caches
        self._jwt_cache: str | None = None
        self._jwt_exp: float = 0.0
        self._install_token: str | None = None
        self._install_token_exp: float = 0.0

        # Per-cycle caches (reset via reset_cycle_cache)
        self._pr_cache: dict[tuple[str, str], bool] = {}
        self._branch_cache: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # ProviderInterface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    def get_branches(self) -> list[str]:
        """List all branch names in the repository (handles pagination)."""
        self._logger.info("[%s] Step: get_branches action=start", self._name)
        if self._auth_method == "pat":
            ready = all([self._owner, self._repo, self._pat])
        else:
            ready = all([self._owner, self._repo, self._private_key, self._app_id])
        if not ready:
            self._logger.error("[%s] Step: get_branches action=error detail=incomplete config"
                               " auth_method=%s", self._name, self._auth_method)
            return []

        out: list[str] = []
        page = 1
        while True:
            self._logger.debug("[%s] Step: get_branches action=fetch page=%d", self._name, page)
            r = self._request("GET", f"{self._repo_root}/branches", params={"per_page": 100, "page": page})
            data = r.json()
            if not data:
                break
            out.extend(b["name"] for b in data)
            if len(data) < 100:
                break
            page += 1

        # Populate branch cache from the full list to avoid redundant API calls later
        for branch_name in out:
            self._branch_cache[branch_name] = True

        self._logger.info("[%s] Step: get_branches action=end total=%d", self._name, len(out))
        return out

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

        r = self._request(
            "GET",
            f"{self._repo_root}/pulls",
            params={
                "state": "open",
                "base": destination,
                "head": f"{self._owner}:{source}",
                "per_page": 1,
            },
        )
        exists = len(r.json()) > 0
        self._pr_cache[key] = exists
        self._logger.info(
            "[%s] Step: check_existing_pr action=end source=%s dest=%s exists=%s",
            self._name, source, destination, str(exists).lower(),
        )
        return exists

    def create_pull_request(self, source: str, destination: str) -> None:
        """Create a PR from source to destination if source branch exists."""
        self._logger.info(
            "[%s] Step: create_pull_request action=start source=%s dest=%s",
            self._name, source, destination,
        )
        if not self._branch_exists(source):
            self._logger.warning(
                "[%s] Step: create_pull_request action=skip source=%s detail=branch not found",
                self._name, source,
            )
            return

        payload = {
            "title": f"Merge {source} into {destination}",
            "head": source,
            "base": destination,
            "body": "Automated PR generated by pr-generator.",
            "draft": False,
        }
        resp = self._request("POST", f"{self._repo_root}/pulls", json=payload)
        self._pr_cache[(source, destination)] = True
        self._logger.info(
            "[%s] Step: create_pull_request action=end source=%s dest=%s"
            " status=created number=%s",
            self._name, source, destination, resp.json().get("number"),
        )

    def reset_cycle_cache(self) -> None:
        """Clear per-cycle branch-existence and PR-existence caches."""
        self._pr_cache.clear()
        self._branch_cache.clear()

    @staticmethod
    def _now() -> float:
        return time.time()

    def _new_jwt(self) -> str:
        self._logger.debug("[GitHub] Step: get_jwt action=generate")
        if not (self._app_id and self._private_key):
            raise RuntimeError("[GitHub] Missing GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY.")
        now = int(self._now())
        payload = {"iat": now - 60, "exp": now + (9 * 60), "iss": self._app_id}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def _get_jwt(self) -> str:
        if self._jwt_cache and self._now() < self._jwt_exp - 30:
            return self._jwt_cache
        self._jwt_cache = self._new_jwt()
        self._jwt_exp = self._now() + (9 * 60)
        return self._jwt_cache

    def _resolve_installation_id(self) -> str:
        self._logger.info("[GitHub] Step: resolve_installation_id action=start")
        if self._installation_id:
            self._logger.info("[GitHub] Step: resolve_installation_id action=end detail=provided")
            return self._installation_id
        r = self._request(
            "GET",
            f"{_API_BASE}/repos/{self._owner}/{self._repo}/installation",
            installation=False,
        )
        inst_id = str(r.json().get("id", ""))
        if not inst_id:
            raise RuntimeError("[GitHub] Could not resolve installation id.")
        # Cache so subsequent token refreshes don't make an extra API call
        self._installation_id = inst_id
        self._logger.info("[GitHub] Step: resolve_installation_id action=end id=%s", inst_id)
        return inst_id

    def _get_installation_token(self) -> str:
        if self._install_token and self._now() < self._install_token_exp - 30:
            return self._install_token
        self._logger.info("[GitHub] Step: get_installation_token action=start")
        inst_id = self._resolve_installation_id()
        r = self._request(
            "POST",
            f"{_API_BASE}/app/installations/{inst_id}/access_tokens",
            installation=False,
        )
        data = r.json()
        self._install_token = data.get("token")
        expires_at = data.get("expires_at", "")
        try:
            self._install_token_exp = datetime.fromisoformat(
                expires_at.replace("Z", "+00:00")
            ).timestamp()
        except Exception as exc:
            self._logger.warning(
                "[%s] Step: get_installation_token action=warn detail=failed to parse expiry (%s); using 55min default",
                self._name, exc,
            )
            self._install_token_exp = self._now() + (55 * 60)
        self._logger.info("[GitHub] Step: get_installation_token action=end")
        return self._install_token

    def _headers(self, installation: bool = True) -> dict:
        if self._auth_method == "pat":
            return {
                "Authorization": f"token {self._pat}",
                "Accept": "application/vnd.github+json",
            }
        if installation:
            return {
                "Authorization": f"Bearer {self._get_installation_token()}",
                "Accept": "application/vnd.github+json",
            }
        return {
            "Authorization": f"Bearer {self._get_jwt()}",
            "Accept": "application/vnd.github+json",
        }

    def _request(self, method: str, url: str, installation: bool = True, **kwargs):
        return request_with_retry(
            logger=self._logger,
            client_name="GitHub",
            method=method,
            url=url,
            timeout=self._timeout,
            headers_factory=lambda: self._headers(installation),
            exception_cls=GitHubError,
            should_retry=self._should_retry,
            **kwargs,
        )

    def _should_retry(self, status_code: int | None, exc: Exception | None) -> bool:
        if exc is not None:
            self._logger.warning("[%s] Retry due to request failure: %s", self._name, exc)
            return True
        return bool(status_code and (500 <= status_code < 600 or status_code in (408, 429)))

    def _branch_exists(self, branch: str) -> bool:
        if branch in self._branch_cache:
            return self._branch_cache[branch]
        self._logger.info("[%s] Step: branch_exists action=start branch=%s", self._name, branch)
        try:
            self._request("GET", f"{self._repo_root}/branches/{branch}")
            self._branch_cache[branch] = True
            self._logger.info("[%s] Step: branch_exists action=end branch=%s exists=true", self._name, branch)
            return True
        except GitHubError as exc:
            if exc.status_code == 404:
                self._branch_cache[branch] = False
                self._logger.info("[%s] Step: branch_exists action=end branch=%s exists=false", self._name, branch)
                return False
            raise
