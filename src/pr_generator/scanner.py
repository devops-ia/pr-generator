"""Scan cycle orchestrator with concurrent rule processing."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from pr_generator.models import AppConfig, CycleResult, RuleResult, ScanRule
from pr_generator.providers.base import ProviderInterface

logger = logging.getLogger("pr_generator.scanner")

_MAX_RULE_WORKERS = 10


def scan_cycle(
    config: AppConfig,
    providers: dict[str, ProviderInterface],
    cycle_id: int,
) -> CycleResult:
    """Execute one full scan cycle.

    Phase 1: Fetch branches from every active provider concurrently.
    Phase 2: Process every rule×provider pair concurrently.
    """
    logger.info(
        "[Core] Step: scan_cycle action=start cycle_id=%d rules=%d providers=%s",
        cycle_id, len(config.rules), list(providers.keys()),
    )

    # Reset per-cycle caches on all providers
    for prov in providers.values():
        prov.reset_cycle_cache()

    # Phase 1 — fetch branches in parallel (one task per provider)
    branches_by_provider: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
        futures = {
            pool.submit(prov.get_branches): prov_name
            for prov_name, prov in providers.items()
        }
        for future in as_completed(futures):
            prov_name = futures[future]
            try:
                branches_by_provider[prov_name] = future.result()
            except Exception as exc:
                logger.error(
                    "[%s] Step: get_branches action=error cycle_id=%d detail=%s",
                    prov_name.capitalize(), cycle_id, exc,
                )
                branches_by_provider[prov_name] = []

    # Phase 2 — process rules × providers in parallel
    result = CycleResult(cycle_id=cycle_id)
    task_futures = []

    with ThreadPoolExecutor(max_workers=_MAX_RULE_WORKERS) as pool:
        for rule in config.rules:
            for prov_name, dest_branch in rule.destinations.items():
                if prov_name not in providers:
                    logger.debug(
                        "[Core] Step: process_rule action=skip rule=%s detail=provider %s not active",
                        rule.pattern, prov_name,
                    )
                    continue
                task_futures.append(pool.submit(
                    _process_rule,
                    provider=providers[prov_name],
                    branches=branches_by_provider.get(prov_name, []),
                    rule=rule,
                    dest_branch=dest_branch,
                    dry_run=config.dry_run,
                    cycle_id=cycle_id,
                ))

        for future in as_completed(task_futures):
            try:
                result.rule_results.append(future.result())
            except Exception as exc:
                logger.error("[Core] Step: process_rule action=error cycle_id=%d detail=%s", cycle_id, exc)

    # Aggregate and log cycle summary
    total = sum(r.processed for r in result.rule_results)
    created = sum(r.created for r in result.rule_results)
    skipped = sum(r.skipped_existing for r in result.rule_results)
    simulated = sum(r.simulated for r in result.rule_results)
    errors = sum(r.errors for r in result.rule_results)
    logger.info(
        "[Core] Step: scan_cycle action=end cycle_id=%d processed=%d"
        " created=%d skipped_existing=%d dry_run=%d errors=%d",
        cycle_id, total, created, skipped, simulated, errors,
    )
    return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _process_rule(
    provider: ProviderInterface,
    branches: list[str],
    rule: ScanRule,
    dest_branch: str,
    dry_run: bool,
    cycle_id: int,
) -> RuleResult:
    """Filter branches by rule and create PRs where needed."""
    pname = provider.name.capitalize()
    result = RuleResult(
        rule_pattern=rule.pattern,
        provider=provider.name,
        destination=dest_branch,
    )
    logger.info(
        "[%s] Step: process_rule action=start cycle_id=%d pattern=%s dest=%s",
        pname, cycle_id, rule.pattern, dest_branch,
    )

    matched = [
        b for b in branches
        if b != dest_branch and rule.compiled.match(b)
    ]

    for branch in matched:
        result.processed += 1
        try:
            if provider.check_existing_pr(branch, dest_branch):
                result.skipped_existing += 1
                continue
            if dry_run:
                logger.info(
                    "[%s] Step: create_pull_request action=dry_run cycle_id=%d source=%s dest=%s",
                    pname, cycle_id, branch, dest_branch,
                )
                result.simulated += 1
                continue
            provider.create_pull_request(branch, dest_branch)
            result.created += 1
        except Exception as exc:
            logger.error(
                "[%s] Step: create_pull_request action=error cycle_id=%d source=%s dest=%s detail=%s",
                pname, cycle_id, branch, dest_branch, exc,
            )
            result.errors += 1

    logger.info(
        "[%s] Step: process_rule action=end cycle_id=%d pattern=%s dest=%s"
        " processed=%d created=%d dry_run=%d skipped=%d errors=%d",
        pname, cycle_id, rule.pattern, dest_branch,
        result.processed, result.created, result.simulated,
        result.skipped_existing, result.errors,
    )
    return result
