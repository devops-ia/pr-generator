"""Health HTTP server exposing /livez, /readyz, /healthz and /metrics endpoints."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pr_generator.metrics import PrGeneratorMetrics

logger = logging.getLogger("pr_generator.health")


class _HealthHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler for Kubernetes health probes and metrics scraping.

    Endpoints:
      /livez, /healthz  → 200 while running; 503 when shutting down.
      /readyz           → 200 after the first full scan cycle; 503 before that.
      /metrics          → 200 with Prometheus text exposition; 404 when metrics
                          are not enabled (``metrics`` is ``None``).
    """

    # Injected by the server factory below
    stop_event: Event
    ready_event: Event
    metrics: PrGeneratorMetrics | None

    def _write(self, code: int, body: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/livez", "/healthz"):
            if self.stop_event.is_set():
                self._write(503, "shutting down")
            else:
                self._write(200, "live")
        elif self.path == "/readyz":
            if self.ready_event.is_set() and not self.stop_event.is_set():
                self._write(200, "ready")
            else:
                self._write(503, "not ready")
        elif self.path == "/metrics":
            if self.metrics is not None:
                from pr_generator.metrics import METRICS_CONTENT_TYPE  # noqa: PLC0415
                body = self.metrics.generate_latest()
                self.send_response(200)
                self.send_header("Content-Type", METRICS_CONTENT_TYPE)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            else:
                self._write(404, "metrics not enabled")
        else:
            self._write(404, "not found")

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        # Suppress default access logs; health probes are very frequent
        pass


def start_health_server(
    port: int,
    stop_event: Event,
    metrics: PrGeneratorMetrics | None = None,
) -> tuple[ThreadingHTTPServer, Event]:
    """Start the health HTTP server in a daemon thread.

    Args:
        port: TCP port to listen on.
        stop_event: Set this event to signal the server that the process is
            shutting down.  ``/livez`` returns 503 once it is set.
        metrics: Optional :class:`~pr_generator.metrics.PrGeneratorMetrics`
            instance.  When provided, ``GET /metrics`` returns Prometheus text
            output.  When ``None``, ``GET /metrics`` returns 404.

    Returns:
        ``(server, ready_event)`` — set *ready_event* after the first
        successful scan cycle to flip ``/readyz`` to 200.
    """
    ready_event = Event()

    # Inject shared state into the handler class via a closure-built subclass
    handler_cls = type(
        "_BoundHealthHandler",
        (_HealthHandler,),
        {"stop_event": stop_event, "ready_event": ready_event, "metrics": metrics},
    )

    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)  # nosec B104
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("[Core] Step: health_server action=start port=%d", port)
    return server, ready_event
