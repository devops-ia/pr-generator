"""Shared HTTP client with retry/backoff logic for all providers."""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

_BACKOFF_DELAYS = (0.5, 1, 2)

ShouldRetry = Callable[[int | None, Exception | None], bool]
HeadersFactory = Callable[[], dict]


def request_with_retry(
    *,
    logger: logging.Logger,
    client_name: str,
    method: str,
    url: str,
    timeout: float,
    exception_cls,
    should_retry: ShouldRetry,
    headers: dict | None = None,
    headers_factory: HeadersFactory | None = None,
    **request_kwargs,
):
    """Execute an HTTP request with shared logging and retry logic.

    Args:
        logger: module logger.
        client_name: human-readable label, e.g. "GitHub".
        method: HTTP verb.
        url: request URL.
        timeout: seconds passed to requests.
        exception_cls: provider-specific exception raised on failure.
            Must accept ``(message: str, status_code: int | None)`` positional args.
        should_retry: predicate receiving (status_code, exception).
        headers: static headers (mutually exclusive with headers_factory).
        headers_factory: callable returning fresh headers per attempt.
        **request_kwargs: forwarded to ``requests.request``.
    """
    if headers is None and headers_factory is None:
        raise ValueError("Provide either headers or headers_factory")

    attempts = (0,) + _BACKOFF_DELAYS
    last_error: Exception | None = None

    for delay in attempts:
        if delay:
            time.sleep(delay)

        hdrs = headers if headers_factory is None else headers_factory()
        try:
            logger.debug(
                "[%s] [HTTP] %s %s params=%s",
                client_name, method, url,
                request_kwargs.get("params"),
            )
            start = time.time()
            response = requests.request(method, url, headers=hdrs, timeout=timeout, **request_kwargs)
            duration_ms = int((time.time() - start) * 1000)
            logger.debug("[%s] [HTTP] %s %s -> %s (%dms)", client_name, method, url, response.status_code, duration_ms)
        except requests.RequestException as exc:
            logger.exception("[%s] [HTTP] %s %s failed: %s", client_name, method, url, exc)
            err = exception_cls(f"Request failure: {exc}", None)
            last_error = err
            if should_retry(None, exc):
                continue
            raise err

        if response.status_code >= 400:
            logger.error("[%s] [HTTP] %s %s error %s: %s", client_name, method, url, response.status_code, response.text)
            err = exception_cls(f"{client_name} API error {response.status_code}: {response.text}", response.status_code)
            last_error = err
            if should_retry(response.status_code, None):
                continue
            raise err

        return response

    if last_error is None:
        raise RuntimeError(f"[{client_name}] request_with_retry exhausted retries with no recorded error")  # pragma: no cover
    raise last_error
