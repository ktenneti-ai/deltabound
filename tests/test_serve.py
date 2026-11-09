"""`deltabound viewer`: local-only server for the built viewer and a source's export files."""
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from deltabound.project import load_project
from deltabound.serve import ServeError, serve, start, viewer_dist

ROOT = Path(__file__).parent.parent


@pytest.fixture
def dist(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>viewer</title>")
    (tmp_path / "pyodide").mkdir()
    (tmp_path / "pyodide" / "x.wasm").write_bytes(b"\0asm")
    return tmp_path


def get(url: str) -> tuple[int, bytes, str]:
    try:
        with urlopen(url) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "")
    except HTTPError as e:
        return e.code, b"", ""


def test_serves_viewer_and_only_shared_files(dist):
    files = load_project(ROOT / "demo" / "sap_bods").export_files()
    server, link = serve(files, port=0, open_browser=False, dist=dist)
    try:
        host = link.split("?")[0]
        assert link.startswith("http://127.0.0.1:") and "?src=local/0/customer_loyalty.xml,local/1/" in link
        assert get(host)[0] == 200
        status, body, ctype = get(host + "pyodide/x.wasm")
        assert (status, ctype) == (200, "application/wasm")
        status, body, _ = get(host + "local/0/customer_loyalty.xml")
        assert status == 200 and body.startswith(b"<?xml")
        assert get(host + "local/9/secret.txt")[0] == 404
        assert get(host + "local/../../deltabound.toml")[0] == 404
        assert get(host + "../pyproject.toml")[0] == 404
    finally:
        server.shutdown()


def test_refuses_non_local_hosts(dist):
    with pytest.raises(ServeError, match="only listens on this machine"):
        start(dist, {}, host="0.0.0.0", port=0)


def test_missing_build_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("DELTABOUND_VIEWER_DIST", str(tmp_path))
    monkeypatch.setattr("deltabound.serve.Path.exists", lambda self: False)
    with pytest.raises(ServeError, match="npm run build"):
        viewer_dist()
