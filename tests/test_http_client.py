"""Tests for the shared HTTP client with retry/backoff logic."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, call, patch

import pytest
import requests

from pr_generator.http_client import request_with_retry


class _TestError(Exception):
    """Stub provider exception used in tests."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_logger = logging.getLogger("test_http_client")


def _make_response(status_code: int, json_data=None, text: str = ""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_data or {}
    return resp


class TestRequestWithRetrySuccess:
    def test_returns_response_on_200(self):
        with patch("requests.request", return_value=_make_response(200, {"ok": True})) as mock_req:
            resp = request_with_retry(
                logger=_logger,
                client_name="Test",
                method="GET",
                url="http://example.com/api",
                timeout=5,
                exception_cls=_TestError,
                should_retry=lambda s, e: False,
                headers={"Authorization": "Bearer tok"},
            )
        assert resp.json() == {"ok": True}
        mock_req.assert_called_once()

    def test_uses_headers_factory_per_attempt(self):
        call_count = 0

        def factory():
            nonlocal call_count
            call_count += 1
            return {"X-Attempt": str(call_count)}

        with patch("requests.request", return_value=_make_response(200)):
            request_with_retry(
                logger=_logger,
                client_name="Test",
                method="GET",
                url="http://example.com",
                timeout=5,
                exception_cls=_TestError,
                should_retry=lambda s, e: False,
                headers_factory=factory,
            )
        assert call_count == 1

    def test_raises_if_neither_headers_nor_factory(self):
        with pytest.raises(ValueError, match="Provide either headers"):
            request_with_retry(
                logger=_logger,
                client_name="Test",
                method="GET",
                url="http://example.com",
                timeout=5,
                exception_cls=_TestError,
                should_retry=lambda s, e: False,
            )


class TestRequestWithRetryHttpErrors:
    def test_raises_provider_exception_on_4xx(self):
        with patch("requests.request", return_value=_make_response(404, text="not found")):
            with pytest.raises(_TestError) as exc_info:
                request_with_retry(
                    logger=_logger,
                    client_name="Test",
                    method="GET",
                    url="http://example.com",
                    timeout=5,
                    exception_cls=_TestError,
                    should_retry=lambda s, e: False,
                    headers={},
                )
        assert exc_info.value.status_code == 404
        assert "404" in str(exc_info.value)

    def test_exception_carries_status_code(self):
        with patch("requests.request", return_value=_make_response(422, text="unprocessable")):
            with pytest.raises(_TestError) as exc_info:
                request_with_retry(
                    logger=_logger,
                    client_name="Test",
                    method="GET",
                    url="http://example.com",
                    timeout=5,
                    exception_cls=_TestError,
                    should_retry=lambda s, e: False,
                    headers={},
                )
        assert exc_info.value.status_code == 422

    def test_retries_on_500_then_succeeds(self):
        responses = [_make_response(500), _make_response(200, {"ok": True})]
        with patch("requests.request", side_effect=responses):
            with patch("time.sleep"):  # skip actual backoff delays
                resp = request_with_retry(
                    logger=_logger,
                    client_name="Test",
                    method="GET",
                    url="http://example.com",
                    timeout=5,
                    exception_cls=_TestError,
                    should_retry=lambda s, e: s is not None and s >= 500,
                    headers={},
                )
        assert resp.json() == {"ok": True}

    def test_raises_after_exhausting_all_retries(self):
        """All 4 attempts return 503 → should raise with the last error."""
        with patch("requests.request", return_value=_make_response(503)):
            with patch("time.sleep"):
                with pytest.raises(_TestError) as exc_info:
                    request_with_retry(
                        logger=_logger,
                        client_name="Test",
                        method="GET",
                        url="http://example.com",
                        timeout=5,
                        exception_cls=_TestError,
                        should_retry=lambda s, e: s is not None and s >= 500,
                        headers={},
                    )
        assert exc_info.value.status_code == 503

    def test_no_retry_on_4xx(self):
        """4xx errors should NOT be retried — only one HTTP call made."""
        with patch("requests.request", return_value=_make_response(400)) as mock_req:
            with pytest.raises(_TestError):
                request_with_retry(
                    logger=_logger,
                    client_name="Test",
                    method="GET",
                    url="http://example.com",
                    timeout=5,
                    exception_cls=_TestError,
                    should_retry=lambda s, e: s is not None and s >= 500,
                    headers={},
                )
        assert mock_req.call_count == 1


class TestRequestWithRetryNetworkErrors:
    def test_raises_on_network_exception(self):
        with patch("requests.request", side_effect=requests.ConnectionError("refused")):
            with pytest.raises(_TestError) as exc_info:
                request_with_retry(
                    logger=_logger,
                    client_name="Test",
                    method="GET",
                    url="http://example.com",
                    timeout=5,
                    exception_cls=_TestError,
                    should_retry=lambda s, e: False,
                    headers={},
                )
        assert exc_info.value.status_code is None
        assert "Request failure" in str(exc_info.value)

    def test_retries_on_network_exception_then_succeeds(self):
        responses = [
            requests.ConnectionError("refused"),
            _make_response(200, {"ok": True}),
        ]
        with patch("requests.request", side_effect=responses):
            with patch("time.sleep"):
                resp = request_with_retry(
                    logger=_logger,
                    client_name="Test",
                    method="GET",
                    url="http://example.com",
                    timeout=5,
                    exception_cls=_TestError,
                    should_retry=lambda s, e: e is not None,
                    headers={},
                )
        assert resp.json() == {"ok": True}

    def test_backoff_delays_are_applied(self):
        """All 4 attempts fail → sleep called 3 times with backoff delays."""
        with patch("requests.request", side_effect=requests.ConnectionError("x")):
            with patch("time.sleep") as mock_sleep:
                with pytest.raises(_TestError):
                    request_with_retry(
                        logger=_logger,
                        client_name="Test",
                        method="GET",
                        url="http://example.com",
                        timeout=5,
                        exception_cls=_TestError,
                        should_retry=lambda s, e: True,
                        headers={},
                    )
        assert mock_sleep.call_count == 3
        assert mock_sleep.call_args_list == [call(0.5), call(1), call(2)]
