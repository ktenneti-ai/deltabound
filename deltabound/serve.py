"""`deltabound viewer`: serve the built web viewer on a local URL and open it.

Binds to 127.0.0.1 only. Serves the viewer's static files plus, optionally, the exact
export files of one source, which the viewer preloads via ?src=. Nothing else on disk is
reachable, and nothing is sent anywhere: the analyzer runs in the browser.
"""
from __future__ import annotations

import mimetypes
import os
import posixpath
import socketserver
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote, urlsplit

DIST_ENV = "DELTABOUND_VIEWER_DIST"
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")


class ServeError(Exception):
    pass


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer.server_bind does a reverse DNS lookup (socket.getfqdn) that can stall for
        # tens of seconds on some networks; a local-only server does not need the name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def viewer_dist() -> Path:
    """The built viewer: $DELTABOUND_VIEWER_DIST, else viewer/dist next to this package (source checkout)."""
    candidates = [Path(os.environ[DIST_ENV])] if os.environ.get(DIST_ENV) else []
    candidates.append(Path(__file__).resolve().parent.parent / "viewer" / "dist")
    for c in candidates:
        if (c / "index.html").exists():
            return c
    raise ServeError("The viewer is not built. Run: cd viewer && npm install && npm run build "
                     f"(or set {DIST_ENV} to a built viewer folder).")


def make_handler(dist: Path, files: dict[str, Path]):
    """files: URL path under /local/ -> file on disk (the only files outside dist that can be read)."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(dist), **kwargs)

        def log_message(self, fmt, *args):  # quiet: one line per request is noise here
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            super().end_headers()

        def do_GET(self):  # noqa: N802 (http.server API)
            path = posixpath.normpath(unquote(urlsplit(self.path).path))
            if path.startswith("/local/"):
                target = files.get(path[len("/local/"):])
                if target is None:
                    self.send_error(404, "Not one of the files shared with the viewer")
                    return
                data = target.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if ".." in path.split("/"):
                self.send_error(404)
                return
            super().do_GET()

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

    return Handler


def shared_files(paths: list[Path]) -> dict[str, Path]:
    """URL names for the files a source contributes (exports, plus explanations.json if present)."""
    out: dict[str, Path] = {}
    for i, p in enumerate(paths):
        out[f"{i}/{p.name}"] = p
    return out


def start(dist: Path, files: dict[str, Path], host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ServeError("The viewer only listens on this machine (127.0.0.1).")
    try:
        server = LocalServer((host, port), make_handler(dist, files))
    except OSError as e:
        raise ServeError(f"Cannot listen on {host}:{port}: {e.strerror}. Try --port 0 for a free port.") from e
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def url(server: ThreadingHTTPServer, files: dict[str, Path]) -> str:
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}/"
    if not files:
        return base
    return base + "?src=" + ",".join("local/" + quote(k) for k in files)


def serve(paths: list[Path], port: int = 8765, open_browser: bool = True,
          dist: Optional[Path] = None) -> tuple[ThreadingHTTPServer, str]:
    dist = dist or viewer_dist()
    files = shared_files(paths)
    server = start(dist, files, port=port)
    link = url(server, files)
    if open_browser:
        webbrowser.open(link)
    return server, link
