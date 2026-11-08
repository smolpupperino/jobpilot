"""Connect to JobPilot's browser container over the DevTools protocol."""

import socket
from contextlib import asynccontextmanager

from app.config import get_settings


@asynccontextmanager
async def cdp():
    """Yields (browser, context). Disconnects on exit; the browser keeps running."""
    from playwright.async_api import async_playwright

    s = get_settings()
    pw = await async_playwright().start()
    try:
        # Chrome rejects DevTools requests whose Host header is a name, so use the IP.
        ip = socket.gethostbyname(s.browser_cdp_host)
        browser = await pw.chromium.connect_over_cdp(f"http://{ip}:{s.browser_cdp_port}", timeout=20000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        yield browser, context
    finally:
        await pw.stop()
