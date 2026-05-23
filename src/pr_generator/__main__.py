"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from threading import Event
from typing import TYPE_CHECKING

from pr_generator.config import load_config
from pr_generator.health import start_health_server
from pr_generator.logging_config import setup_logging
from pr_generator.metrics import PrGeneratorMetrics
from pr_generator.models import ScanRule
from pr_generator.providers.base import ProviderInterface
from pr_generator.providers.bitbucket import BitbucketProvider
from pr_generator.providers.github import GitHubProvider
from pr_generator.scanner import scan_cycle

if TYPE_CHECKING:
    from pr_generator.annotation_discovery import AnnotationDiscoveryClient

logger = logging.getLogger("pr_generator")


def main() -> None:
    """Entry point: load config, start health server, run scan loop."""
    parser = argparse.ArgumentParser(
        description="Automated PR creation daemon for GitHub and Bitbucket Cloud.",
    )
    try:
        _version = pkg_version("pr-generator")
    except PackageNotFoundError:
        _version = "unknown"
    parser.add_argument(
        "--version",
        action="version",
        version=f"pr-generator {_version}",
    )
    parser.parse_args()

    # Bootstrap logging with a sensible default before config is loaded
    setup_logging("INFO")

    try:
        config = load_config()
    except (ValueError, FileNotFoundError) as exc:
        logger.error("[Core] Step: startup action=error detail=%s", exc)
        sys.exit(1)

    # Re-configure logging with the level and format from config
    setup_logging(config.log_level, json_format=(config.log_format == "json"))

    # Instantiate active providers
    providers: dict[str, ProviderInterface] = {}
    for pname, pconf in config.providers.items():
        if not pconf.enabled:
            continue
        if pconf.type == "github":
            providers[pname] = GitHubProvider(pconf)
        elif pconf.type == "bitbucket":
            providers[pname] = BitbucketProvider(pconf)
        else:
            logger.warning("[Core] Unknown provider type '%s' for '%s'; skipping.", pconf.type, pname)

    if not providers:
        logger.warning("[Core] Step: startup action=warn detail=No active providers configured; running in idle mode")

    # Graceful shutdown
    stop = Event()

    def _handler(sig, _frame) -> None:
        logger.info("[Core] Received signal %s; initiating graceful shutdown.", sig)
        stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    # Health server + metrics (readiness flips after first cycle)
    metrics = PrGeneratorMetrics()
    _server, ready_event = start_health_server(config.health_port, stop, metrics=metrics)

    logger.info("[Core] Active providers: %s", ", ".join(providers.keys()))
    logger.info("[Core] Rules configured: %d", len(config.rules))
    for rule in config.rules:
        logger.info("[Core] Rule: pattern=%s destinations=%s", rule.pattern, rule.destinations)
    if config.dry_run:
        logger.info("[Core] Dry-run mode enabled — PR creations will only be logged")

    # Initialise annotation discovery client once at startup (lazy import so
    # the kubernetes package is only required when the feature is enabled)
    annotation_enabled = config.annotation_mode in {"annotations_only", "hybrid"}
    discovery_client: AnnotationDiscoveryClient | None = None
    if annotation_enabled:
        from pr_generator.annotation_discovery import AnnotationDiscoveryClient  # noqa: PLC0415
        try:
            discovery_client = AnnotationDiscoveryClient.from_incluster()
        except RuntimeError as exc:
            logger.error("[Core] Step: startup action=error detail=%s", exc)
            sys.exit(1)

    cycle_id = 0
    while not stop.is_set():
        cycle_id += 1
        effective_rules = _resolve_rules(config.rules, config.annotation_mode, discovery_client, config.annotation_prefix, cycle_id, metrics=metrics)

        metrics.rules_active.set(len(effective_rules))
        scan_cycle(config, providers, cycle_id, effective_rules=effective_rules, metrics=metrics)

        if not ready_event.is_set():
            ready_event.set()
            logger.info("[Core] Ready state achieved (first cycle completed)")

        _sleep_interval(config.scan_frequency, stop)

    logger.info("[Core] Shutdown complete.")


def _resolve_rules(
    static_rules: list[ScanRule],
    annotation_mode: str,
    discovery_client: AnnotationDiscoveryClient | None,
    annotation_prefix: str,
    cycle_id: int,
    metrics: PrGeneratorMetrics | None = None,
) -> list[ScanRule]:
    """Resolve the effective rule set for a scan cycle.

    Args:
        static_rules: Rules loaded from ``config.yaml``.
        annotation_mode: One of ``"config_only"``, ``"annotations_only"``,
            or ``"hybrid"``.
        discovery_client: Initialised :class:`~pr_generator.annotation_discovery.AnnotationDiscoveryClient`,
            or ``None`` when annotation discovery is disabled.
        annotation_prefix: Annotation key prefix (e.g. ``"pr-generator.io"``).
        cycle_id: Current cycle identifier, used only for log messages.
        metrics: Optional :class:`~pr_generator.metrics.PrGeneratorMetrics`
            instance.  When provided, updates the annotation rules discovered gauge.

    Returns:
        The list of :class:`~pr_generator.models.ScanRule` objects to use for
        this cycle.
    """
    if annotation_mode == "config_only" or discovery_client is None:
        if metrics is not None:
            metrics.record_annotation_rules(0)
        return static_rules

    from pr_generator.annotation_discovery import merge_rules  # noqa: PLC0415

    annotation_rules = discovery_client.list_application_rules(annotation_prefix)

    if metrics is not None:
        metrics.record_annotation_rules(len(annotation_rules))

    if annotation_mode == "annotations_only":
        effective = annotation_rules
    else:  # hybrid
        effective = merge_rules(static_rules, annotation_rules)

    logger.info(
        "[Core] Step: cycle action=rules_resolved cycle_id=%d static=%d annotation=%d effective=%d",
        cycle_id, len(static_rules), len(annotation_rules), len(effective),
    )
    return effective


def _sleep_interval(total: int, stop: Event) -> None:
    """Sleep in ≤1 s slices to react quickly to stop signals."""
    waited = 0
    while waited < total and not stop.is_set():
        stop.wait(timeout=min(1, total - waited))
        waited += 1


if __name__ == "__main__":
    main()
