"""Health HTTP server exposing /livez, /readyz and /healthz endpoints."""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event

logger = logging.getLogger("pr_generator.health")


class _HealthHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler for Kubernetes health probes.

    Endpoints:
      /livez, /healthz  → 200 while running; 503 when shutting down.
      /readyz           → 200 after the first full scan cycle; 503 before that.
    """

    # Injected by the server factory below
    stop_event: Event
    ready_event: Event

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
        else:
            self._write(404, "not found")

    def log_message(self, fmt: str, *args) -> None:  # noqa: ANN002
        # Suppress default access logs; health probes are very frequent
        pass


def start_health_server(port: int, stop_event: Event) -> tuple[ThreadingHTTPServer, Event]:
    """Start the health HTTP server in a daemon thread.

    Returns:
        (server, ready_event) — set ready_event after the first successful cycle.
    """
    ready_event = Event()

    # Inject shared state into the handler class via a closure-built subclass
    handler_cls = type(
        "_BoundHealthHandler",
        (_HealthHandler,),
        {"stop_event": stop_event, "ready_event": ready_event},
    )

    server = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    logger.info("[Core] Step: health_server action=start port=%d", port)
    return server, ready_event
