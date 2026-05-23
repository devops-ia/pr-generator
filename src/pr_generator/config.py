"""Configuration loading from YAML file."""

from __future__ import annotations

import base64
import logging
import os
import re

import yaml

from pr_generator.models import AnnotationMode, AppConfig, ProviderConfig, ScanRule

logger = logging.getLogger("pr_generator.config")

_DEFAULT_CONFIG_PATH = "/etc/pr-generator/config.yaml"


def load_config() -> AppConfig:
    """Load application configuration from a YAML file.

    The config file path defaults to /etc/pr-generator/config.yaml and can be
    overridden with the CONFIG_PATH environment variable.
    """
    config_path = os.getenv("CONFIG_PATH", _DEFAULT_CONFIG_PATH)
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"[Core] Config file not found at '{config_path}'. "
            "Set CONFIG_PATH to the correct path or create the file."
        )
    logger.info("[Core] Step: load_config action=start source=file path=%s", config_path)
    return _load_from_file(config_path)


# ------------------------------------------------------------------
# YAML-based loading
# ------------------------------------------------------------------

def _load_from_file(path: str) -> AppConfig:
    with open(path) as fh:
        raw = yaml.safe_load(fh)

    raw = raw or {}
    providers = _parse_providers_from_yaml(raw.get("providers") or {})
    rules = _parse_rules(raw.get("rules") or [])
    annotation_mode, annotation_prefix = _parse_annotation_discovery(raw.get("annotation_discovery") or {})

    if not rules:
        if annotation_mode == "config_only":
            raise ValueError("[Core] config.yaml has no rules defined.")
        logger.info(
            "[Core] Step: load_config action=warn detail=no static rules; "
            "annotation_mode=%s will supply rules at runtime", annotation_mode,
        )
    if not providers:
        logger.info("[Core] Step: load_config action=warn detail=no enabled providers configured; running in idle mode")

    config = AppConfig(
        scan_frequency=int(raw.get("scan_frequency", 300)),
        log_level=str(raw.get("log_level", "INFO")),
        log_format=str(raw.get("log_format", "text")).lower(),
        dry_run=bool(raw.get("dry_run", False)),
        health_port=int(raw.get("health_port", 8080)),
        providers=providers,
        rules=rules,
        annotation_mode=annotation_mode,
        annotation_prefix=annotation_prefix,
    )
    logger.info(
        "[Core] Step: load_config action=end source=file providers=%s rules=%d annotation_mode=%s",
        list(providers.keys()), len(rules), annotation_mode,
    )
    return config


def _parse_providers_from_yaml(raw: dict) -> dict[str, ProviderConfig]:
    """Parse the providers section of the YAML config.

    Each key is a provider *name* (e.g. ``github``, ``github-acme``, ``bitbucket``).
    The optional ``type`` field selects the provider implementation; it defaults to
    the key name for the two built-in values ``"github"`` and ``"bitbucket"`` to keep
    backward compatibility with existing configs.

    Example — multiple GitHub orgs::

        providers:
          github-acme:
            type: github
            enabled: true
            owner: acme-org
            repo: backend
            ...
          github-skunkworks:
            type: github
            enabled: true
            owner: skunkworks-org
            repo: platform
            ...
    """
    providers: dict[str, ProviderConfig] = {}

    for pname, pcfg in raw.items():
        if not isinstance(pcfg, dict):
            continue
        if not pcfg.get("enabled", False):
            continue

        # Resolve type: explicit field wins; fall back to key name for known types.
        ptype = str(pcfg.get("type", "")).lower() or (
            pname if pname in {"github", "bitbucket"} else ""
        )
        if ptype not in {"github", "bitbucket"}:
            raise ValueError(
                f"[Core] Provider '{pname}' has unknown or missing type '{ptype}'. "
                "Set 'type: github' or 'type: bitbucket'."
            )

        if ptype == "github":
            providers[pname] = _parse_github_provider(pname, pcfg)
        else:
            providers[pname] = _parse_bitbucket_provider(pname, pcfg)

    _validate_token_env_uniqueness(raw)
    return providers


def _validate_token_env_uniqueness(raw: dict) -> None:
    """Raise if two providers of the same type share a tokenEnv value.

    A duplicated tokenEnv means only one provider's credential ends up in the
    container environment — the other silently gets the wrong token.
    """
    seen: dict[str, str] = {}  # tokenEnv value -> first provider name that uses it
    for pname, pcfg in raw.items():
        if not isinstance(pcfg, dict) or not pcfg.get("enabled", False):
            continue
        ptype = str(pcfg.get("type", "")).lower() or (
            pname if pname in {"github", "bitbucket"} else ""
        )
        if ptype == "github" and str(pcfg.get("auth_method", "app")).lower() == "pat":
            env = str(pcfg.get("token_env", "GITHUB_TOKEN"))
        elif ptype == "bitbucket":
            env = str(pcfg.get("token_env", "BITBUCKET_TOKEN"))
        else:
            continue
        if env in seen:
            raise ValueError(
                f"[Core] Providers '{seen[env]}' and '{pname}' both use tokenEnv '{env}'. "
                "Each provider must use a unique env var name to avoid credential collisions."
            )
        seen[env] = pname


def _parse_github_provider(name: str, gh: dict) -> ProviderConfig:
    """Build a ProviderConfig for a GitHub provider entry."""
    auth_method = str(gh.get("auth_method", "app")).lower()
    owner = str(gh.get("owner", "")).strip()
    repo = str(gh.get("repo", "")).strip()
    if not owner or not repo:
        raise ValueError(
            f"[Core] Provider '{name}': 'owner' and 'repo' are required fields. "
            f"Check providers.{name} in your config.yaml."
        )
    if auth_method == "pat":
        token_env = str(gh.get("token_env", "GITHUB_TOKEN"))
        token = os.getenv(token_env, "")
        if not token:
            raise ValueError(
                f"[Core] Provider '{name}': env var '{token_env}' is empty or not set. "
                f"Set {token_env} with a valid GitHub PAT."
            )
        return ProviderConfig(
            name=name,
            type="github",
            enabled=True,
            owner=owner,
            repo=repo,
            auth_method="pat",
            token=token,
            timeout=float(gh.get("timeout", 30)),
        )
    app_id = str(gh.get("app_id", "")).strip()
    if not app_id:
        raise ValueError(
            f"[Core] Provider '{name}': 'app_id' is required for GitHub App auth. "
            f"Check providers.{name} in your config.yaml."
        )
    private_key = _load_private_key(gh)
    if not private_key:
        raise ValueError(
            f"[Core] Provider '{name}': no private key found. "
            f"Set 'private_key_path' in config or the GITHUB_APP_PRIVATE_KEY env var."
        )
    return ProviderConfig(
        name=name,
        type="github",
        enabled=True,
        owner=owner,
        repo=repo,
        app_id=app_id,
        installation_id=str(gh.get("installation_id", "")),
        private_key=private_key,
        auth_method="app",
        timeout=float(gh.get("timeout", 30)),
    )


def _parse_bitbucket_provider(name: str, bb: dict) -> ProviderConfig:
    """Build a ProviderConfig for a Bitbucket provider entry."""
    workspace = str(bb.get("workspace", "")).strip()
    repo_slug = str(bb.get("repo_slug", "")).strip()
    if not workspace or not repo_slug:
        raise ValueError(
            f"[Core] Provider '{name}': 'workspace' and 'repo_slug' are required fields. "
            f"Check providers.{name} in your config.yaml."
        )
    token_env = str(bb.get("token_env", "BITBUCKET_TOKEN"))
    token = os.getenv(token_env, "")
    if not token:
        raise ValueError(
            f"[Core] Provider '{name}': env var '{token_env}' is empty or not set. "
            f"Set {token_env} with a valid Bitbucket access token."
        )
    return ProviderConfig(
        name=name,
        type="bitbucket",
        enabled=True,
        workspace=workspace,
        repo_slug=repo_slug,
        token=token,
        timeout=float(bb.get("timeout", 30)),
        close_source_branch=bool(bb.get("close_source_branch", True)),
    )


def _load_private_key(gh_cfg: dict) -> str:
    """Load GitHub App private key from file path or env var.

    If ``private_key_path`` is set but the file does not exist, a
    ``FileNotFoundError`` is raised immediately — no silent fallthrough to the
    env-var fallback.  That fallback is only tried when ``private_key_path`` is
    absent or empty.
    """
    key_path = str(gh_cfg.get("private_key_path", ""))
    if key_path:
        if not os.path.exists(key_path):
            raise FileNotFoundError(
                f"[Core] private_key_path '{key_path}' does not exist. "
                "Check the mounted secret volume or correct the path in config.yaml."
            )
        with open(key_path) as fh:
            return fh.read()

    # Fallback: try env var (supports base64-encoded PEM)
    raw = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if raw and "-----BEGIN" not in raw:
        raw = base64.b64decode(raw).decode()
    return raw


def _parse_rules(raw_rules: list) -> list[ScanRule]:
    rules: list[ScanRule] = []
    for item in raw_rules:
        if not isinstance(item, dict):
            logger.warning("[Core] Step: load_config action=warn detail=rule entry is not a mapping; skipping")
            continue
        pattern = str(item.get("pattern", ""))
        if not pattern:
            logger.warning("[Core] Step: load_config action=warn detail=rule with empty pattern; skipping")
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"[Core] Invalid regex pattern '{pattern}': {exc}") from exc
        destinations = {str(k): str(v) for k, v in (item.get("destinations") or {}).items()}
        if not destinations:
            logger.warning("[Core] Step: load_config action=warn detail=rule pattern=%s has no destinations; skipping", pattern)
            continue
        rules.append(ScanRule(pattern=pattern, compiled=compiled, destinations=destinations))
    return rules


_VALID_ANNOTATION_MODES: frozenset[str] = frozenset({"config_only", "annotations_only", "hybrid"})


def _parse_annotation_discovery(raw: dict) -> tuple[AnnotationMode, str]:
    """Parse the ``annotation_discovery`` section of config.yaml.

    Args:
        raw: Parsed YAML dict for the ``annotation_discovery`` key.
            May be empty; all fields have safe defaults.

    Returns:
        A ``(annotation_mode, annotation_prefix)`` tuple.
        Defaults to ``("config_only", "pr-generator.io")`` when the section
        is absent or empty.

    Raises:
        ValueError: When ``mode`` is not one of the accepted values.
    """
    mode = str(raw.get("mode", "config_only")).lower()
    if mode not in _VALID_ANNOTATION_MODES:
        raise ValueError(
            f"[Core] annotation_discovery.mode '{mode}' is invalid. "
            f"Valid values: {sorted(_VALID_ANNOTATION_MODES)}."
        )
    prefix = str(raw.get("annotation_prefix", "pr-generator.io")).strip().rstrip("/")
    return mode, prefix  # type: ignore[return-value]  # narrowed by membership check above

