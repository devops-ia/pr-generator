"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from importlib.metadata import version as pkg_version
from threading import Event

from pr_generator.config import load_config
from pr_generator.health import start_health_server
from pr_generator.logging_config import setup_logging
from pr_generator.providers.bitbucket import BitbucketProvider
from pr_generator.providers.github import GitHubProvider
from pr_generator.scanner import scan_cycle

logger = logging.getLogger("pr_generator")


def main() -> None:
    """Entry point: load config, start health server, run scan loop."""
    parser = argparse.ArgumentParser(
        description="Automated PR creation daemon for GitHub and Bitbucket Cloud.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"pr-generator {pkg_version('pr-generator')}",
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
    providers = {}
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

    def _handler(sig, _frame):
        logger.info("[Core] Received signal %s; initiating graceful shutdown.", sig)
        stop.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)

    # Health server (readiness flips after first cycle)
    _server, ready_event = start_health_server(config.health_port, stop)

    logger.info("[Core] Active providers: %s", ", ".join(providers.keys()))
    logger.info("[Core] Rules configured: %d", len(config.rules))
    for rule in config.rules:
        logger.info("[Core] Rule: pattern=%s destinations=%s", rule.pattern, rule.destinations)
    if config.dry_run:
        logger.info("[Core] Dry-run mode enabled — PR creations will only be logged")

    cycle_id = 0
    while not stop.is_set():
        cycle_id += 1
        cycle_start = time.time()
        scan_cycle(config, providers, cycle_id)
        duration = time.time() - cycle_start
        logger.info("[Core] Step: cycle action=complete cycle_id=%d duration_sec=%.1f", cycle_id, duration)

        if not ready_event.is_set():
            ready_event.set()
            logger.info("[Core] Ready state achieved (first cycle completed)")

        _sleep_interval(config.scan_frequency, stop)

    logger.info("[Core] Shutdown complete.")


def _sleep_interval(total: int, stop: Event) -> None:
    """Sleep in ≤1 s slices to react quickly to stop signals."""
    waited = 0
    while waited < total and not stop.is_set():
        stop.wait(timeout=min(1, total - waited))
        waited += 1


if __name__ == "__main__":
    main()
