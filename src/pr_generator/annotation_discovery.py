"""Annotation-based rule discovery from ArgoCD Application resources.

Reads ``applications.argoproj.io`` cluster-wide and extracts :class:`~pr_generator.models.ScanRule`
objects from Kubernetes annotations.  Only Applications that carry the opt-in
annotation ``<prefix>/enabled: "true"`` are processed.

Annotation schema (default prefix ``pr-generator.io``):

.. code-block:: yaml

   metadata:
     annotations:
       pr-generator.io/enabled: "true"
       pr-generator.io/pattern: "argocd-image-updater-.*-dev-.*"
       pr-generator.io/destination.github: "develop"
       pr-generator.io/destination.bitbucket: "dev"

The ``destination.<provider-key>`` suffix maps directly to provider keys defined
under ``config.providers`` (e.g. ``github``, ``bitbucket``, ``github-acme``).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pr_generator.models import ScanRule

logger = logging.getLogger(__name__)

_ARGOCD_GROUP = "argoproj.io"
_ARGOCD_VERSION = "v1alpha1"
_ARGOCD_PLURAL = "applications"


class AnnotationDiscoveryClient:
    """Discovers :class:`~pr_generator.models.ScanRule` objects from ArgoCD
    Application annotations cluster-wide.

    Instantiate via :meth:`from_incluster` when running inside a Kubernetes pod,
    or pass a pre-built ``CustomObjectsApi`` instance directly for testing.

    Example:
        >>> client = AnnotationDiscoveryClient.from_incluster()
        >>> rules = client.list_application_rules("pr-generator.io")
    """

    def __init__(self, api: Any) -> None:
        """
        Args:
            api: A ``kubernetes.client.CustomObjectsApi`` instance (or any
                object with a compatible ``list_cluster_custom_object`` method).
        """
        self._api = api

    @classmethod
    def from_incluster(cls) -> AnnotationDiscoveryClient:
        """Create a client using in-cluster Kubernetes credentials.

        Loads the ServiceAccount token and CA certificate automatically from
        the standard pod mount paths.

        Returns:
            A configured :class:`AnnotationDiscoveryClient`.

        Raises:
            RuntimeError: If the in-cluster configuration cannot be loaded,
                e.g. missing ServiceAccount token or insufficient RBAC permissions.
        """
        try:
            from kubernetes import client, config as k8s_config  # type: ignore[import]
            k8s_config.load_incluster_config()
            api = client.CustomObjectsApi()
        except Exception as exc:
            raise RuntimeError(
                f"[AnnotationDiscovery] Failed to initialise in-cluster Kubernetes "
                f"client: {exc}. Ensure the pod has a ServiceAccount with ClusterRole "
                f"access to {_ARGOCD_GROUP}/{_ARGOCD_PLURAL} and that "
                f"automountServiceAccountToken is enabled on the pod spec."
            ) from exc

        logger.info("[AnnotationDiscovery] Step: init action=success detail=in-cluster config loaded")
        return cls(api)

    def list_application_rules(self, prefix: str) -> list[ScanRule]:
        """Discover ScanRules from ArgoCD Application annotations cluster-wide.

        Queries the Kubernetes API for all ``applications.argoproj.io`` resources
        and converts annotation-annotated ones into :class:`~pr_generator.models.ScanRule`
        objects.  API errors are logged and an empty list is returned so that the
        scan cycle degrades gracefully rather than crashing.

        Args:
            prefix: Annotation key prefix without a trailing slash
                (e.g. ``"pr-generator.io"``).

        Returns:
            One :class:`~pr_generator.models.ScanRule` per Application that has
            ``<prefix>/enabled: "true"`` and at least one valid
            ``<prefix>/destination.<provider>`` annotation.
            Returns ``[]`` on Kubernetes API errors.
        """
        enabled_key = f"{prefix}/enabled"
        pattern_key = f"{prefix}/pattern"
        dest_prefix = f"{prefix}/destination."

        try:
            response: dict[str, Any] = self._api.list_cluster_custom_object(
                group=_ARGOCD_GROUP,
                version=_ARGOCD_VERSION,
                plural=_ARGOCD_PLURAL,
            )
        except Exception as exc:
            logger.error(
                "[AnnotationDiscovery] Step: list_applications action=error detail=%s", exc
            )
            return []

        items: list[dict[str, Any]] = response.get("items") or []
        rules = [
            rule
            for app in items
            if (rule := self._parse_application(app, enabled_key, pattern_key, dest_prefix))
            is not None
        ]

        logger.info(
            "[AnnotationDiscovery] Step: list_applications action=end "
            "total_apps=%d rules_discovered=%d",
            len(items),
            len(rules),
        )
        return rules

    def _parse_application(
        self,
        app: dict[str, Any],
        enabled_key: str,
        pattern_key: str,
        dest_prefix: str,
    ) -> ScanRule | None:
        """Parse a single ArgoCD Application dict into a ScanRule.

        Args:
            app: Raw Application resource dict from the Kubernetes API.
            enabled_key: Full annotation key for the opt-in flag
                (e.g. ``"pr-generator.io/enabled"``).
            pattern_key: Full annotation key for the branch regex pattern.
            dest_prefix: Full annotation prefix for destination entries
                (e.g. ``"pr-generator.io/destination."``).

        Returns:
            A :class:`~pr_generator.models.ScanRule` when the Application has
            valid annotations, or ``None`` when it should be skipped.
        """
        meta: dict[str, Any] = app.get("metadata") or {}
        name: str = meta.get("name", "<unknown>")
        namespace: str = meta.get("namespace", "")
        annotations: dict[str, str] = meta.get("annotations") or {}

        if annotations.get(enabled_key, "").lower() != "true":
            logger.debug(
                "[AnnotationDiscovery] app=%s/%s skipped (annotation %s != 'true')",
                namespace, name, enabled_key,
            )
            return None

        pattern = annotations.get(pattern_key, "")
        if not pattern:
            logger.warning(
                "[AnnotationDiscovery] app=%s/%s skipped: missing annotation %s",
                namespace, name, pattern_key,
            )
            return None

        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            logger.warning(
                "[AnnotationDiscovery] app=%s/%s skipped: invalid regex '%s': %s",
                namespace, name, pattern, exc,
            )
            return None

        destinations: dict[str, str] = {
            key[len(dest_prefix):]: value
            for key, value in annotations.items()
            if key.startswith(dest_prefix) and value
        }
        if not destinations:
            logger.warning(
                "[AnnotationDiscovery] app=%s/%s skipped: no destination.* annotations "
                "(expected prefix '%s')",
                namespace, name, dest_prefix,
            )
            return None

        logger.info(
            "[AnnotationDiscovery] app=%s/%s discovered pattern=%s destinations=%s",
            namespace, name, pattern, destinations,
        )
        return ScanRule(pattern=pattern, compiled=compiled, destinations=destinations)


def merge_rules(
    static: list[ScanRule],
    annotation: list[ScanRule],
) -> list[ScanRule]:
    """Merge static config rules with annotation-derived rules for hybrid mode.

    Annotation rules take precedence at the *destination* level: if both sources
    define the same ``pattern`` + provider pair, the annotation value wins.
    Distinct patterns and providers are additive — annotations can override only
    the ``github`` destination while keeping the static ``bitbucket`` destination.

    Args:
        static: Rules loaded from ``config.yaml``.
        annotation: Rules discovered from ArgoCD Application annotations.

    Returns:
        Merged list of :class:`~pr_generator.models.ScanRule` objects. Order
        follows the static list, with annotation-only patterns appended at the end.

    Example:
        >>> import re
        >>> from pr_generator.models import ScanRule
        >>> s = [ScanRule("feat/.*", re.compile("feat/.*"), {"github": "old", "bitbucket": "dev"})]
        >>> a = [ScanRule("feat/.*", re.compile("feat/.*"), {"github": "new"})]
        >>> merged = merge_rules(s, a)
        >>> merged[0].destinations
        {'github': 'new', 'bitbucket': 'dev'}
    """
    merged: dict[str, ScanRule] = {r.pattern: r for r in static}

    for ann_rule in annotation:
        existing = merged.get(ann_rule.pattern)
        if existing is None:
            merged[ann_rule.pattern] = ann_rule
            continue

        combined = {**existing.destinations, **ann_rule.destinations}
        overridden = {
            k: f"{existing.destinations[k]!r} -> {ann_rule.destinations[k]!r}"
            for k in ann_rule.destinations
            if k in existing.destinations
            and existing.destinations[k] != ann_rule.destinations[k]
        }
        if overridden:
            logger.debug(
                "[AnnotationDiscovery] hybrid merge: pattern=%s annotation overrides %s",
                ann_rule.pattern, overridden,
            )
        merged[ann_rule.pattern] = ScanRule(
            pattern=existing.pattern,
            compiled=existing.compiled,
            destinations=combined,
        )

    return list(merged.values())
