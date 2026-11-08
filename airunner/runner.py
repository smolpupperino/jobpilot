#!/usr/bin/env python3
"""JobPilot AI runner: a tiny host-side service that runs the claude / codex / gemini CLIs.

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


# --- Gemini ---------------------------------------------------------------------
# Every tool denied by policy (shell, files, web, MCP): Gemini only answers.
GEMINI_POLICY = """
[[rule]]
toolName = "*"
decision = "deny"
priority = 999
denyMessage = "Tools are disabled. Answer with JSON only."

[[rule]]
toolName = "*"
mcpName = "*"
decision = "deny"
priority = 999
"""
GEMINI_CREDS = os.path.expanduser("~/.gemini/oauth_creds.json")


def _gemini_env() -> dict:
    node_bin = os.path.expanduser("~/.local/opt/node/bin")
    return dict(os.environ, PATH=f"{node_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}", NO_BROWSER="true",
                GEMINI_CLI_NO_RELAUNCH="true")


def _json_from_text(text: str) -> dict:
    """The JSON object in a model reply (tolerates ```json fences and chatter around it)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise AIError("gemini returned no JSON")
    try:
        out = json.loads(text[start:end + 1])
    except ValueError as e:
        raise AIError("gemini returned invalid JSON") from e
    if not isinstance(out, dict):
        raise AIError("gemini returned non-object JSON")
    return out


def run_gemini(model: str, effort: str, system: str, prompt: str, schema: dict, timeout: int, cwd: str,
               images: list[str]) -> dict:
    policy = os.path.join(cwd, "deny-all.toml")
    with open(policy, "w") as f:
        f.write(GEMINI_POLICY)
    # JobPilot's instructions replace Gemini's own agent prompt (which talks about tools).
    system_md = os.path.join(cwd, "system.md")
    with open(system_md, "w") as f:
        f.write(system + "\n\nReply with ONE JSON object only (no prose, no code fences) that matches this "
                "JSON schema:\n" + json.dumps(schema))
    env = dict(_gemini_env(), GEMINI_SYSTEM_MD=system_md)
    cmd = [_bin("gemini"), "-p", "Answer the request given on stdin.", "-o", "json", "--policy", policy,
           "--approval-mode", "default"]
    if model:
        cmd += ["-m", model]
    # Screenshots aren't passed to Gemini (that would need its file tool); it reads the page JSON.
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as e:
        raise AIError(f"timed out after {timeout}s") from e
    try:
        out = json.loads(p.stdout)
    except ValueError:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-2:]
        raise AIError(f"gemini exit {p.returncode}: {' | '.join(tail)[:300]}") from None
    if out.get("error"):
        raise AIError(f"gemini: {str(out['error'].get('message', out['error']))[:300]}")
    return _json_from_text(str(out.get("response", "")))


class GeminiLogin:
    """Google sign-in for the Gemini CLI, driven through a pseudo-terminal so the owner can
    do it from JobPilot's Settings: start() returns Google's link, finish(code) types the code."""

    def __init__(self) -> None:
        self.pid: int | None = None
        self.fd: int | None = None
        self.lock = threading.Lock()

    def _stop(self) -> None:
        import signal

        if self.pid:
            try:
                os.kill(self.pid, signal.SIGTERM)
                os.waitpid(self.pid, 0)
            except (OSError, ChildProcessError):
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.pid = self.fd = None

    def _read(self, seconds: float, until) -> str:
        import re
        import select

        buf, end = b"", time.monotonic() + seconds
        while time.monotonic() < end:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if r:
                try:
                    chunk = os.read(self.fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", buf.decode("utf-8", "replace"))
            if until(text):
                return text
        return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", buf.decode("utf-8", "replace"))

    def start(self) -> dict:
        import fcntl
        import pty
        import re
        import struct
        import termios

        with self.lock:
            self._stop()
            os.makedirs(os.path.dirname(GEMINI_CREDS), exist_ok=True)
            settings = os.path.expanduser("~/.gemini/settings.json")
            try:
                with open(settings) as f:
                    conf = json.load(f)
            except (OSError, ValueError):
                conf = {}
            conf.setdefault("security", {}).setdefault("auth", {})["selectedType"] = "oauth-personal"
            with open(settings, "w") as f:
                json.dump(conf, f, indent=2)
            home = tempfile.mkdtemp(prefix="jp-gemini-login-")
            pid, fd = pty.fork()
            if pid == 0:
                os.chdir(home)
                os.execvpe(_bin("gemini"), [_bin("gemini")], _gemini_env())
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 60, 1000, 0, 0))  # wide: URL on one line
            self.pid, self.fd = pid, fd
            pat = re.compile(r"https://accounts\.google\.com/o/oauth2/\S+")
            text = self._read(45, lambda t: bool(pat.search(t)))
            m = pat.search(text)
            if not m:
                self._stop()
                raise AIError("Gemini didn't show a Google sign-in link")
            return {"url": m.group(0)}

    def finish(self, code: str) -> dict:
        code = (code or "").strip()
        if not code or len(code) > 500 or any(c.isspace() for c in code):
            raise AIError("paste the code Google showed you")
        with self.lock:
            if self.fd is None:
                raise AIError("start the Gemini sign-in first")
            os.write(self.fd, code.encode() + b"\r")
            self._read(30, lambda t: os.path.exists(GEMINI_CREDS))
            ok = os.path.exists(GEMINI_CREDS)
            self._stop()
            if not ok:
                raise AIError("Google didn't accept that code. Start again and use the new code.")
            return {"signed_in": True}


gemini_login = GeminiLogin()

RUNNERS = {"claude": run_claude, "codex": run_codex, "gemini": run_gemini}


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
            self._reply(200, {"ok": True, "providers": have, "gemini_signed_in": os.path.exists(GEMINI_CREDS)})
        elif self.path == "/v1/gemini/models":
            try:
                p = subprocess.run([_bin("gemini"), "--list-models"], capture_output=True, text=True, timeout=60,
                                   env=_gemini_env(), cwd=tempfile.gettempdir())
                models = [ln.split()[0].strip("-*• ") for ln in p.stdout.splitlines()
                          if ln.strip().lower().lstrip("-*• ").startswith("gemini")]
                self._reply(200, {"ok": True, "models": [m for m in dict.fromkeys(models) if m][:30]})
            except (AIError, OSError, subprocess.TimeoutExpired) as e:
                self._reply(200, {"ok": False, "error": str(e)[:300]})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path in ("/v1/gemini/login/start", "/v1/gemini/login/finish"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length)) if 0 < length < 10000 else {}
                if self.path.endswith("start"):
                    out = gemini_login.start()
                else:
                    out = gemini_login.finish(str(body.get("code", "")))
                self._reply(200, {"ok": True, **out})
            except (AIError, OSError, ValueError) as e:
                self._reply(200, {"ok": False, "error": str(e)[:300]})
            sys.stderr.write(f"gemini login {self.path.rsplit('/', 1)[-1]}\n")  # never the code
            return
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
