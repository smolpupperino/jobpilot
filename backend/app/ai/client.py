"""Call an AI model through the host-side AI runner (airunner/runner.py).

Every call is stateless: the caller sends everything the model needs, and gets back
JSON matching a schema. The provider/model/effort come from the owner's per-task
settings, read fresh on every call, so changing them takes effect on the next call.
The fallback route is tried if the primary fails.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings
from app.db import session_factory
from app.events import log_event
from app.settings_store import AISettings, TaskRoute, get_setting

log = logging.getLogger("jobpilot.ai")


class AIUnavailable(Exception):
    pass


@dataclass
class AIResult:
    output: dict[str, Any]
    provider: str
    model: str
    effort: str
    ms: int


def _routes(task: str) -> list[tuple[str, str, str]]:
    with session_factory()() as db:
        route: TaskRoute = get_setting(db, "ai", AISettings).tasks.get(task) or TaskRoute()
    out = []
    if route.provider != "none":
        out.append((route.provider, route.model, route.effort))
    if route.fallback_provider != "none":
        out.append((route.fallback_provider, route.fallback_model, route.fallback_effort))
    return out


async def runner_request(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    """Any other call to the host AI runner (e.g. Gemini sign-in). Never raises on HTTP errors."""
    transport = httpx.AsyncHTTPTransport(uds=str(get_settings().ai_socket))
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
            r = await client.request(method, f"http://ai{path}", json=body)
            return r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"ok": False, "error": f"AI runner unreachable ({type(e).__name__})"}


async def _post(payload: dict, timeout: float) -> dict:
    sock = str(get_settings().ai_socket)
    transport = httpx.AsyncHTTPTransport(uds=sock)
    async with httpx.AsyncClient(transport=transport, timeout=timeout) as client:
        r = await client.post("http://ai/v1/run", json=payload)
        return r.json()


async def run_task(
    task: str, system: str, prompt: str, schema: dict, job_id: int | None = None, timeout: int = 240,
    images: list[bytes] | None = None, prefer_fallback: bool = False,
) -> AIResult:
    import base64

    routes = _routes(task)
    if prefer_fallback and len(routes) > 1:
        routes = routes[1:] + routes[:1]
    if not routes:
        raise AIUnavailable(f"No AI provider is set for {task} (Settings -> AI models)")
    errors = []
    for provider, model, effort in routes:
        t0 = time.monotonic()
        payload = {"provider": provider, "model": model, "effort": effort, "system": system,
                   "prompt": prompt, "schema": schema, "timeout": timeout,
                   "images": [base64.b64encode(i).decode() for i in (images or [])]}
        try:
            body = await _post(payload, timeout + 30)
        except (httpx.HTTPError, ValueError) as e:
            body = {"ok": False, "error": f"AI runner unreachable ({type(e).__name__}). Is it running?"}
        ms = int((time.monotonic() - t0) * 1000)
        ok = bool(body.get("ok")) and isinstance(body.get("output"), dict)
        # Log metadata only: never prompts or outputs.
        with session_factory()() as db:
            log_event(db, "system", "ai.call", job_id=job_id, task=task, provider=provider,
                      model=model or "default", effort=effort or "default", ms=ms, ok=ok,
                      error=None if ok else str(body.get("error"))[:300])
            db.commit()
        if ok:
            return AIResult(body["output"], provider, model, effort, ms)
        errors.append(f"{provider}/{model or 'default'}: {body.get('error')}")
        log.warning("AI call failed for %s: %s", task, errors[-1][:300])
    raise AIUnavailable("; ".join(errors)[:800])
