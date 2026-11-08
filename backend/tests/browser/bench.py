"""Browser-agent benchmark on the local fixture site (never a real employer site).

    scripts/test-browser.sh bench                       # stand-in model (deterministic, free)
    BENCH_DOCKER_ARGS="-v $PWD/local-run:/run/jobpilot-ai" \\
      scripts/test-browser.sh bench --live --provider codex --model gpt-6.1-terra --effort low \\
      --fallback-provider codex --fallback-model gpt-6-luna      # the owner's real model via the AI runner

Three parts:
1. widgets: each hard dropdown, PREVIOUS executor (worker._select_combo, up to 3 tries, as the old
   loop allowed before asking the owner) vs HYBRID (one fast try, then the adaptive agent).
2. pages: stuck-page scenarios for the agent's "get unstuck" mode (the old path had no executor for
   these: it re-planned with a screenshot, then asked the owner).
3. application: the whole fixture application through the real worker, flag off vs on.
Results are measured, never estimated; anything not run is reported as not run.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("JOBPILOT_SESSION_SECRET", "bench-secret-" + "x" * 40)
os.environ.setdefault("JOBPILOT_VAULT_KEY", "q83vEi8nLc0sX5U8yK5b9Yb1kU2tF9mJ3wQ7Zp1aR6c=")
os.environ["JOBPILOT_COOKIE_SECURE"] = "false"
os.environ.pop("JOBPILOT_TELEGRAM_BOT_TOKEN", None)

PAGE_CASES = {  # name -> what "done" means (JS that must be true afterwards), or an expected stop category
    "modal": "!document.querySelector('[role=dialog]')",
    "add_section": "document.querySelectorAll('#jobs input').length === 1",
    "obscured": "document.querySelector('h1').textContent === 'Step 2'",
    "failed_click": "document.querySelector('h1').textContent === 'Step 2'",
    "missing_button": "document.querySelector('h1').textContent === 'Step 2'",
    "delayed": "!!document.querySelector('#c button')",
    "unresponsive": "document.querySelector('h1').textContent === 'Step 2'",
    "spa": "document.querySelector('h2').textContent === 'Questions'",
    "captcha": "stop:captcha_required",
    "error500": "stop:site_server_error",
}


def fresh_db() -> Path:
    import app.models  # noqa: F401  (registers the tables)
    from app import db as dbmod
    from app.config import get_settings
    from app.db import Base

    d = Path(tempfile.mkdtemp(prefix="jp-bench-"))
    os.environ["JOBPILOT_DATA_DIR"] = str(d)
    get_settings.cache_clear()
    eng = dbmod.init_engine(f"sqlite:///{d / 'bench.db'}")
    Base.metadata.create_all(eng)
    return d


def set_route(args) -> None:
    from app.db import session_factory
    from app.settings_store import AISettings, TaskRoute, put_setting

    with session_factory()() as db:
        put_setting(db, "ai", AISettings(tasks={"form_mapper": TaskRoute(
            provider=args.provider, model=args.model, effort=args.effort,
            fallback_provider=args.fallback_provider, fallback_model=args.fallback_model)}))
        db.commit()


def make_decider(args):
    from tests.browser.policy import HeuristicDecider

    if not args.live:
        return HeuristicDecider()
    from app.apply.agent import ai_decider

    class Live:
        calls = 0

        async def __call__(self, *a):
            self.calls += 1
            return await ai_decider(*a)
    return Live()


async def bench_widgets(browser, site, args) -> list[dict]:
    from app.apply.agent import AdaptiveAgent
    from app.apply.worker import ApplyError, _select_combo
    from tests.browser.fixtures import DROPDOWN_GOALS, DROPDOWNS

    rows = []
    for name, (value, expected) in DROPDOWN_GOALS.items():
        cfg = DROPDOWNS[name]
        for mode in ("previous", "hybrid"):
            ctx = await browser.new_context(viewport={"width": 1100, "height": 800})
            page = await ctx.new_page()
            await page.goto(site.at(name))
            loc = page.locator(f"#dd_{cfg['name']}")
            t0 = time.monotonic()
            row = {"scenario": name, "mode": mode, "ai_calls": 0, "ai_ms": 0, "actions": 0, "failed_actions": 0,
                   "recoveries": 0, "repeats_blocked": 0, "fast_tries": 0}
            ok = False
            tries = 3 if mode == "previous" else 1
            for _ in range(tries):
                row["fast_tries"] += 1
                try:
                    await asyncio.wait_for(_select_combo(page, loc, value), 60)
                except (ApplyError, Exception):  # noqa: BLE001, S110
                    pass
                committed = await page.locator(f"input[name={cfg['name']}]").input_value()
                if expected and expected in committed.split("|"):
                    ok = True
                    break
            if mode == "previous":
                row["ai_calls"] = row["fast_tries"] - 1  # each retry = one planner re-plan in the old loop
            if mode == "hybrid" and not ok:
                d = make_decider(args)
                out = await AdaptiveAgent(page, decider=d).operate_field(loc, cfg["label"], value, "combobox")
                row.update(ai_calls=out.ai_calls, ai_ms=out.ai_ms, actions=out.actions,
                           failed_actions=out.failed_actions, recoveries=out.recoveries,
                           repeats_blocked=out.repeats_blocked, agent_success=out.success,
                           fallback=out.fallback_used)
            committed = await page.locator(f"input[name={cfg['name']}]").input_value()
            row["committed"] = committed
            row["correct"] = (expected in committed.split("|")) if expected else committed == ""
            row["completed"] = bool(expected) and row["correct"]
            row["needs_human"] = bool(expected) and not row["correct"]
            row["seconds"] = round(time.monotonic() - t0, 1)
            rows.append(row)
            await ctx.close()
            print(json.dumps(row), file=sys.stderr, flush=True)
    return rows


async def bench_pages(browser, site, args) -> list[dict]:
    from app.apply.agent import AdaptiveAgent

    rows = []
    for name, done in PAGE_CASES.items():
        ctx = await browser.new_context(viewport={"width": 1100, "height": 800})
        page = await ctx.new_page()
        await page.goto(site.at(name))
        await page.wait_for_timeout(500)
        t0 = time.monotonic()
        out = await AdaptiveAgent(page, decider=make_decider(args)).unstick("the application isn't moving")
        if done.startswith("stop:"):
            ok = out.category == done[5:] and not out.progressed
        else:
            ok = bool(await page.evaluate(f"() => {done}"))
        row = {"scenario": name, "mode": "agent", "completed": ok, "expected": done, "category": out.category,
               "ai_calls": out.ai_calls, "ai_ms": out.ai_ms, "actions": out.actions,
               "failed_actions": out.failed_actions, "recoveries": out.recoveries,
               "repeats_blocked": out.repeats_blocked, "fallback": out.fallback_used,
               "seconds": round(time.monotonic() - t0, 1)}
        rows.append(row)
        await ctx.close()
        print(json.dumps(row), file=sys.stderr, flush=True)
    return rows


async def bench_application(site, chromium, args) -> list[dict]:
    import pytest

    from tests.browser.harness import EXPECTED_POST, run_application

    rows = []
    for agent in (False, True):
        tmp = fresh_db()
        if args.live:
            set_route(args)
        mp = pytest.MonkeyPatch()
        try:
            r = await run_application(mp, tmp, site, chromium, agent=agent, decider=make_decider(args))
        finally:
            mp.undo()
        wrong = {k: r.posted.get(k) for k, v in EXPECTED_POST.items() if r.posted.get(k) != v}
        summary = next((e for e in r.events if e["type"] == "browser.summary"), {})
        row = {"scenario": "app_spa (whole application)", "mode": "agent on" if agent else "previous",
               "outcome": r.outcome, "completed": r.outcome == "confirmed" and not wrong,
               "submitted_values_correct": bool(r.posted) and not wrong, "wrong_values": wrong,
               "planner_calls": r.counters.planner_calls, "agent_calls": r.counters.agent_calls,
               "takeovers": r.counters.takeovers, "questions": len(r.counters.questions),
               "agent_actions": summary.get("actions", 0), "agent_failed_actions": summary.get("failed_actions", 0),
               "seconds": round(r.seconds, 1)}
        rows.append(row)
        print(json.dumps(row), file=sys.stderr, flush=True)
    return rows


def table(rows: list[dict], cols: list[str]) -> str:
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    return "\n".join(out)


def totals(rows: list[dict], mode: str) -> dict:
    rs = [r for r in rows if r["mode"] == mode]
    return {"mode": mode, "scenarios": len(rs), "completed": sum(1 for r in rs if r.get("completed")),
            "correct": sum(1 for r in rs if r.get("correct", r.get("completed"))),
            "needs_human": sum(1 for r in rs if r.get("needs_human")),
            "ai_calls": sum(r.get("ai_calls", 0) for r in rs), "ai_seconds": round(sum(r.get("ai_ms", 0)
                                                                                        for r in rs) / 1000, 1),
            "failed_actions": sum(r.get("failed_actions", 0) for r in rs),
            "recoveries": sum(r.get("recoveries", 0) for r in rs),
            "repeats_blocked": sum(r.get("repeats_blocked", 0) for r in rs),
            "seconds": round(sum(r.get("seconds", 0) for r in rs), 1)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="use the real AI runner for the agent's decisions")
    ap.add_argument("--provider", default="claude")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--effort", default="")
    ap.add_argument("--fallback-provider", default="none")
    ap.add_argument("--fallback-model", default="")
    ap.add_argument("--parts", default="widgets,pages,application")
    args = ap.parse_args()

    from playwright.async_api import async_playwright

    from tests.browser.conftest import _Handler, chromium_path

    fresh_db()
    if args.live:
        set_route(args)
    from http.server import ThreadingHTTPServer
    from threading import Thread

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.posts = []  # type: ignore[attr-defined]
    Thread(target=srv.serve_forever, daemon=True).start()

    class Site:
        server = srv

        @staticmethod
        def at(n: str) -> str:
            return f"http://127.0.0.1:{srv.server_address[1]}/{n}"

    result: dict = {"live": args.live, "model": f"{args.provider}/{args.model or 'default'}"
                    + (f" effort={args.effort}" if args.effort else "")
                    + (f", fallback {args.fallback_provider}/{args.fallback_model}"
                       if args.fallback_provider != "none" else "") if args.live else "stand-in (tests/browser/policy.py)"}
    parts = args.parts.split(",")
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=chromium_path(), args=["--no-sandbox", "--disable-gpu"])
        if "widgets" in parts:
            result["widgets"] = await bench_widgets(b, Site, args)
        if "pages" in parts:
            result["pages"] = await bench_pages(b, Site, args)
        await b.close()
    if "application" in parts:
        result["application"] = await bench_application(Site, chromium_path(), args)
    srv.shutdown()

    print(f"\n## Benchmark ({result['model']})\n")
    if "widgets" in result:
        w = result["widgets"]
        print("### Dropdowns: previous executor vs hybrid\n")
        print(table([totals(w, "previous"), totals(w, "hybrid")],
                    ["mode", "scenarios", "completed", "correct", "needs_human", "ai_calls", "ai_seconds",
                     "failed_actions", "recoveries", "repeats_blocked", "seconds"]))
        print()
        print(table(w, ["scenario", "mode", "completed", "correct", "fast_tries", "ai_calls", "actions",
                        "failed_actions", "recoveries", "repeats_blocked", "seconds", "committed"]))
    if "pages" in result:
        print("\n### Stuck pages: agent \"get unstuck\" mode\n")
        print(table(result["pages"], ["scenario", "completed", "expected", "category", "ai_calls", "actions",
                                      "failed_actions", "recoveries", "seconds"]))
    if "application" in result:
        print("\n### Whole application through the real worker\n")
        print(table(result["application"], ["mode", "outcome", "completed", "submitted_values_correct",
                                            "planner_calls", "agent_calls", "agent_actions", "agent_failed_actions",
                                            "takeovers", "questions", "seconds"]))
    print("\nRAW_JSON " + json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
