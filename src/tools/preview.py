"""Web Preview tool — serve static files for live preview.

Agents call this to start a sandbox HTTP server that serves generated
HTML/CSS/JS files for the user to preview in the browser.

The server runs in a background thread and serves from a specified directory.
Port allocation is managed within a configurable range.
"""

from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

from src.core.schema import PreviewResult

DEFAULT_PORT_RANGE = (9000, 9100)


class PreviewServer:
    """Manages a background HTTP server for static file preview.

    Usage::

        server = PreviewServer()
        result = server.start(serve_dir="/tmp/preview")
        print(result.url)  # http://localhost:9000
        # ... user views the page ...
        server.stop()
    """

    def __init__(self, port_range: tuple[int, int] = DEFAULT_PORT_RANGE) -> None:
        self._port_range = port_range
        self._httpd: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None
        self._serve_dir: str = ""
        self._port: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self, serve_dir: str | Path, port: int | None = None) -> PreviewResult:
        """Start the preview server.

        Args:
            serve_dir: Directory to serve static files from.
            port: Specific port to use. If None, auto-select from port_range.

        Returns:
            ``PreviewResult`` with URL and status.

        Raises:
            FileNotFoundError: If ``serve_dir`` doesn't exist.
            OSError: If no port is available.
        """
        serve_path = Path(serve_dir).resolve()
        if not serve_path.exists():
            raise FileNotFoundError(f"Directory not found: {serve_path}")

        self._serve_dir = str(serve_path)

        # Pick a port
        if port is not None:
            self._port = port
        else:
            self._port = self._find_free_port()

        # Create handler bound to the serve directory
        serve_dir_str = str(serve_path)

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=serve_dir_str, **kwargs)

            def log_message(self, format, *args):
                pass  # suppress logs

        try:
            self._httpd = socketserver.TCPServer(("", self._port), Handler)
        except OSError:
            # Port in use — try next
            if port is None:
                self._port = self._find_free_port(start=self._port + 1)
                self._httpd = socketserver.TCPServer(("", self._port), Handler)
            else:
                raise

        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

        return PreviewResult(
            url=f"http://localhost:{self._port}",
            port=self._port,
            status="running",
        )

    def stop(self) -> PreviewResult:
        """Stop the preview server."""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

        return PreviewResult(
            url=f"http://localhost:{self._port}",
            port=self._port,
            status="stopped",
        )

    @property
    def is_running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _find_free_port(self, start: int | None = None) -> int:
        """Find a free port in the configured range."""
        lo, hi = self._port_range
        candidates = list(range(start or lo, hi + 1))
        for p in candidates:
            try:
                s = socketserver.TCPServer(("", p), http.server.SimpleHTTPRequestHandler)
                s.server_close()
                return p
            except OSError:
                continue
        raise OSError(f"No free port available in range {lo}-{hi}")
