"""Tests for the health server."""

import time
import urllib.request
from threading import Event

import pytest

from pr_generator.health import start_health_server

_PORT = 18081


@pytest.fixture(scope="module")
def health_server():
    stop = Event()
    server, ready = start_health_server(_PORT, stop)
    time.sleep(0.1)
    yield stop, ready
    stop.set()
    server.shutdown()


def _get(path: str) -> int:
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{_PORT}{path}", timeout=2)
        return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


class TestHealthServer:
    def test_livez_returns_200(self, health_server):
        stop, _ready = health_server
        assert _get("/livez") == 200

    def test_healthz_alias(self, health_server):
        stop, _ready = health_server
        assert _get("/healthz") == 200

    def test_readyz_returns_503_before_ready(self, health_server):
        stop, ready = health_server
        ready.clear()
        assert _get("/readyz") == 503

    def test_readyz_returns_200_after_ready(self, health_server):
        stop, ready = health_server
        ready.set()
        assert _get("/readyz") == 200

    def test_livez_returns_503_when_stopping(self, health_server):
        stop, _ready = health_server
        stop.set()
        assert _get("/livez") == 503
        stop.clear()  # reset for other tests

    def test_unknown_path_returns_404(self, health_server):
        assert _get("/unknown") == 404
