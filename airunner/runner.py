#!/usr/bin/env python3
"""JobPilot AI runner: a tiny host-side service that runs the claude / codex CLIs.

Why it exists: the CLIs and their subscription logins live on the host, not in
the app container. The container talks to this service over a Unix socket that
is mounted into it. Nothing listens on the network.

What it allows: exactly one operation, "answer this prompt as JSON matching this
schema", with every agent tool turned OFF (no shell, no file access, no web, no
plugins). Job pages and forms are untrusted text; with tools off, a malicious
page can at worst produce a wrong answer, which the app validates anyway.

Stdlib only, so it runs on the host's system Python with nothing to install.

Request:  POST /v1/run  {"provider", "model", "effort", "system", "prompt", "schema", "timeout"}
Response: {"ok": true, "output": {...}, "ms": 1234} or {"ok": false, "error": "..."}
"""

import base64
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler

SOCKET_PATH = os.environ.get("JOBPILOT_AI_SOCKET", os.path.expanduser("~/jobpilot/local-run/ai.sock"))
MAX_BODY = 12 * 1024 * 1024  # room for up to 2 screenshots
MAX_IMAGES = 2
MAX_CONCURRENT = 2
EFFORTS = {"", "low", "medium", "high", "xhigh", "max", "ultra"}

# Everything Codex could use to act on the machine or the internet, switched off.
CODEX_DISABLE = [
    "shell_tool", "unified_exec", "apps", "browser_use", "browser_use_external",
    "computer_use", "in_app_browser", "plugins", "remote_plugin", "skill_search",
    "tool_suggest", "sleep_tool", "shell_snapshot",
]

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)


class AIError(Exception):
    pass


def _bin(name: str) -> str:
    path = shutil.which(name) or os.path.expanduser(f"~/.local/bin/{name}")
    if not os.path.exists(path):
        raise AIError(f"{name} CLI not found on the host")
    return path


def _run(cmd: list[str], stdin: str, timeout: int, cwd: str) -> str:
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        raise AIError(f"timed out after {timeout}s") from e
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        raise AIError(f"exit {p.returncode}: {' | '.join(tail)[:400]}")
    return p.stdout


def run_claude(model: str, effort: str, system: str, prompt: str, schema: dict, timeout: int, cwd: str,
               images: list[str]) -> dict:
    cmd = [
        _bin("claude"), "-p", "--tools", "", "--no-session-persistence",
        "--json-schema", json.dumps(schema), "--system-prompt", system,
    ]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if images:
        # Images go in as a stream-json user message (image blocks + the text prompt).
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b}}
                   for b in images] + [{"type": "text", "text": prompt}]
        msg = {"type": "user", "message": {"role": "user", "content": content}}
        cmd += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        lines = _run(cmd, json.dumps(msg) + "\n", timeout, cwd).splitlines()
        results = [json.loads(ln) for ln in lines if ln.startswith("{") and '"type":"result"' in ln.replace(" ", "")]
        if not results:
            raise AIError("claude returned no result")
        out = results[-1]
    else:
        cmd += ["--output-format", "json"]
        out = json.loads(_run(cmd, prompt, timeout, cwd))
    if out.get("is_error"):
        raise AIError(str(out.get("result", "claude error"))[:400])
    result = out.get("structured_output")
    if not isinstance(result, dict):
        raise AIError("claude returned no structured output")
    return result


def run_codex(model: str, effort: str, system: str, prompt: str, schema: dict, timeout: int, cwd: str,
              images: list[str]) -> dict:
    schema_file = os.path.join(cwd, "schema.json")
    out_file = os.path.join(cwd, "out.json")
    with open(schema_file, "w") as f:
        json.dump(schema, f)
    image_args = []
    for i, b in enumerate(images):
        path = os.path.join(cwd, f"screen{i}.png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(b))
        image_args += ["-i", path]
    cmd = [
        _bin("codex"), "exec", *image_args, "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
        "--ignore-rules", "-s", "read-only", "-C", cwd, "-c", 'web_search="disabled"',
        "--output-schema", schema_file, "-o", out_file,
    ]
    for feat in CODEX_DISABLE:
        cmd += ["--disable", feat]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    cmd.append("-")
    _run(cmd, f"{system}\n\n---\n\n{prompt}", timeout, cwd)
    try:
        with open(out_file) as f:
            result = json.loads(f.read())
    except (OSError, ValueError) as e:
        raise AIError("codex returned no valid JSON") from e
    if not isinstance(result, dict):
        raise AIError("codex returned non-object JSON")
    return result


RUNNERS = {"claude": run_claude, "codex": run_codex}


def handle(req: dict) -> dict:
    provider = req.get("provider")
    if provider not in RUNNERS:
        raise AIError(f"unsupported provider: {provider!r}")
    model = str(req.get("model") or "")
    effort = str(req.get("effort") or "")
    if effort not in EFFORTS or len(model) > 100 or not all(c.isalnum() or c in "-._" for c in model):
        raise AIError("bad model or effort")
    system, prompt, schema = req.get("system"), req.get("prompt"), req.get("schema")
    if not isinstance(system, str) or not isinstance(prompt, str) or not isinstance(schema, dict):
        raise AIError("system, prompt and schema are required")
    timeout = max(10, min(int(req.get("timeout") or 180), 600))
    images = req.get("images") or []
    if not isinstance(images, list) or len(images) > MAX_IMAGES or not all(
            isinstance(b, str) and len(b) < 6_000_000 for b in images):
        raise AIError("bad images")
    for b in images:
        if not base64.b64decode(b)[:8] == b"\x89PNG\r\n\x1a\n":
            raise AIError("images must be PNG")
    if not _slots.acquire(timeout=timeout):
        raise AIError("busy")
    try:
        # Fresh empty working dir per call: nothing for the model to see, nothing kept.
        with tempfile.TemporaryDirectory(prefix="jp-ai-") as cwd:
            return RUNNERS[provider](model, effort, system, prompt, schema, timeout, cwd, images)
    finally:
        _slots.release()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/v1/health":
            have = {p: bool(shutil.which(p) or os.path.exists(os.path.expanduser(f"~/.local/bin/{p}")))
                    for p in RUNNERS}
            self._reply(200, {"ok": True, "providers": have})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/run":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._reply(413, {"ok": False, "error": "body too large"})
            return
        t0 = time.monotonic()
        req = None
        try:
            req = json.loads(self.rfile.read(length))
            out = handle(req)
            self._reply(200, {"ok": True, "output": out, "ms": int((time.monotonic() - t0) * 1000)})
        except (AIError, ValueError, TypeError) as e:
            self._reply(200, {"ok": False, "error": str(e)[:500], "ms": int((time.monotonic() - t0) * 1000)})
        # Log metadata only: never prompts or outputs (they contain personal data).
        sys.stderr.write(f"run provider={req.get('provider') if isinstance(req, dict) else '?'} "
                         f"ms={int((time.monotonic() - t0) * 1000)}\n")

    def log_message(self, *args) -> None:  # silence default per-request logging
        pass

    # BaseHTTPRequestHandler expects a (host, port) client address.
    def address_string(self) -> str:
        return "unix"


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def main() -> None:
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o700, exist_ok=True)
    if os.path.exists(SOCKET_PATH):
        os.unlink(SOCKET_PATH)
    old = os.umask(0o177)  # socket is owner-only (the app container runs as the same uid)
    try:
        server = UnixHTTPServer(SOCKET_PATH, Handler)
    finally:
        os.umask(old)
    sys.stderr.write(f"JobPilot AI runner listening on {SOCKET_PATH}\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
