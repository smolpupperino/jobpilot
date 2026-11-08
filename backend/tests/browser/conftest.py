"""Real-browser tests: a headless Chromium against local fixture pages.

Runs where a Chromium is available (scripts/test.sh runs these in the test-browser image,
tests/browser/Dockerfile). Never touches JobPilot's live browser or any real site.
"""

import os
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.browser.fixtures import PAGES

STATIC = Path(__file__).parent / "static"


def chromium_path() -> str | None:
    p = os.environ.get("JOBPILOT_TEST_CHROMIUM") or shutil.which("chromium") or shutil.which("chromium-browser")
    return p if p and os.path.exists(p) else None


if chromium_path() is None:
    collect_ignore_glob = ["test_*.py"]


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/static/"):
            f = STATIC / path.removeprefix("/static/")
            if f.is_file():
                return self._send(200, f.read_bytes(), "application/javascript")
        if path == "/error500":
            return self._send(500, b"<h1>500 Internal Server Error</h1>", "text/html")
        page = PAGES.get(path.strip("/") or "index")
        if page is None:
            return self._send(404, b"<h1>Not found</h1>", "text/html")
        port = self.server.server_address[1]
        html = page.replace("{{OTHER_ORIGIN}}", f"http://localhost:{port}")
        self._send(200, html.encode(), "text/html; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode(errors="replace")
        self.server.posts.append((self.path, body))  # type: ignore[attr-defined]
        self._send(200, b"<h1>Thank you for applying!</h1><p>We have received your application.</p>", "text/html")

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a) -> None:
        pass


@pytest.fixture(scope="session")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.posts = []  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    class Site:
        url = base
        server = srv

        @staticmethod
        def at(name: str) -> str:
            return f"{base}/{name}"

    yield Site
    srv.shutdown()


@pytest.fixture
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox", "--disable-gpu"])
        yield b
        await b.close()


@pytest.fixture
async def page(browser):
    ctx = await browser.new_context(viewport={"width": 1100, "height": 800})
    pg = await ctx.new_page()
    pg.set_default_timeout(10000)
    yield pg
    await ctx.close()
