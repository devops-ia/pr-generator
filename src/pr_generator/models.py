"""Data models shared across the application."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderConfig:
    """Immutable configuration for a single Git provider instance."""

    name: str
    enabled: bool
    type: str = ""               # "github" | "bitbucket" — provider class to use
    timeout: float = 30.0
    # GitHub — common
    owner: str = ""
    repo: str = ""
    auth_method: str = "app"     # "app" (GitHub App) | "pat" (Personal Access Token)
    # GitHub App auth
    app_id: str = ""
    installation_id: str = ""
    private_key: str = ""        # PEM content (loaded at startup)
    # Bitbucket / GitHub PAT
    workspace: str = ""
    repo_slug: str = ""
    token: str = ""              # Bearer/PAT token
    # Bitbucket behaviour
    close_source_branch: bool = True


@dataclass
class ScanRule:
    """A scanning rule: one regex pattern and its destination branch per provider."""

    pattern: str
    compiled: re.Pattern
    destinations: dict[str, str] = field(default_factory=dict)
    # e.g. {"github": "develop", "bitbucket": "nonpro"}


@dataclass(frozen=True)
class AppConfig:
    """Full application configuration."""

    scan_frequency: int
    log_level: str
    log_format: str          # "text" | "json"
    dry_run: bool
    health_port: int
    providers: dict[str, ProviderConfig]   # "github" | "bitbucket" → ProviderConfig
    rules: list[ScanRule]


@dataclass
class RuleResult:
    """Outcome of processing one ScanRule for one provider in a cycle."""

    rule_pattern: str
    provider: str
    destination: str
    processed: int = 0
    created: int = 0
    skipped_existing: int = 0
    simulated: int = 0
    errors: int = 0


@dataclass
class CycleResult:
    """Aggregated outcome of a full scan cycle."""

    cycle_id: int
    rule_results: list[RuleResult] = field(default_factory=list)
