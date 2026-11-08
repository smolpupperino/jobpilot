"""Stand-ins for the AI model in browser tests.

HeuristicDecider plays a small, not-very-clever model. It reads ONLY the prompt text the real
model gets (goal, memory, the observation's snapshot) and has no access to the DOM or the
fixture's code. If the observation leaves something out, or the tools give poor feedback, it
fails just like a model would, so these tests check the observe/act/verify/recover loop for
real while staying deterministic and free.
"""

import re

from app.apply.tools import matches_value

LINE = re.compile(r'^\s*- (?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)(?::\s*(?P<text>.*))?$')
DISMISS = re.compile(r"^(no thanks|not now|close|dismiss|skip|later|maybe later|x|×|no, thanks)$", re.I)
FORWARD = re.compile(r"^(next|continue|start|apply|show more|load more|add)\b", re.I)


def parse(prompt: str) -> dict:
    goal = prompt.split("GOAL\n", 1)[1].split("\n", 1)[0] if "GOAL\n" in prompt else ""
    m = re.search(r"APPROVED VALUE \(.*?\): '(.*)'", prompt) or re.search(r'APPROVED VALUE \(.*?\): "(.*)"', prompt)
    value = m.group(1) if m else ""
    tr = re.search(r"TARGET CONTROL: ref=(\S+)", prompt)
    mem = prompt.split("MEMORY", 1)[1].split("OBSERVATION", 1)[0] if "MEMORY" in prompt else ""
    obs = prompt.split("ACCESSIBILITY SNAPSHOT", 1)[1] if "ACCESSIBILITY SNAPSHOT" in prompt else ""
    nodes, section = [], "page"
    for line in obs.splitlines():
        if line.startswith("# around the target"):
            section = "target"
            continue
        if line.startswith("# open popup"):
            section = "popup"
            continue
        n = LINE.match(line)
        if not n:
            continue
        attrs = n.group("attrs") or ""
        ref = re.search(r"\[ref=(\w+)\]", attrs)
        box = re.search(r"\[box=([\d.-]+),([\d.-]+),([\d.]+),([\d.]+)\]", attrs)
        nodes.append({"role": n.group("role"), "name": n.group("name") or "", "text": (n.group("text") or "").strip(),
                      "ref": ref.group(1) if ref else "", "attrs": attrs, "section": section,
                      "box": [float(x) for x in box.groups()] if box else None})
    focused = re.search(r"FOCUSED: (\{.*\})", prompt)
    images = re.findall(r"IMAGE (\d+): (\w+) screenshot", prompt)
    return {"goal": goal, "value": value, "target": tr.group(1) if tr else "", "memory": mem, "nodes": nodes,
            "focused": focused.group(1) if focused else "", "page_mode": "WITHOUT entering any data" in goal,
            "images": images, "expanded": "EXPANDED" in prompt}


def tried(mem: str, pattern: str) -> int:
    return len(re.findall(pattern, mem, re.I))


def act(type_: str, **kw) -> dict:
    base = {"type": type_, "ref": "", "text": "", "key": "", "option": "", "x": 0, "y": 0, "image": 0,
            "direction": "", "method": "", "expect": ""}
    base.update(kw)
    return base


def reply(*actions, status="continue", thought="") -> dict:
    return {"thought": thought, "status": status, "actions": list(actions)}


def decide(prompt: str) -> dict:
    p = parse(prompt)
    if p["page_mode"]:
        return decide_page(p)
    return decide_field(p)


def decide_page(p: dict) -> dict:
    mem = p["memory"]
    for n in p["nodes"]:
        label = n["name"] or n["text"]
        if n["role"] == "button" and DISMISS.match(label.strip()) and n["ref"]:
            if tried(mem, rf'click button "{re.escape(label)}".*FAILED') < 1:
                return reply(act("click", ref=n["ref"], expect="popup closes"))
    if "dialog" in " ".join(n["role"] for n in p["nodes"]) and tried(mem, r"press_key.*Escape") < 1:
        return reply(act("press_key", key="Escape", expect="popup closes"))
    for n in p["nodes"]:
        label = n["name"] or n["text"]
        if n["role"] in ("button", "link") and FORWARD.match(label) and n["ref"]:
            fails = tried(mem, rf'"{re.escape(label)}".*FAILED')
            if fails == 0:
                return reply(act("click", ref=n["ref"], expect="page moves on"))
            if fails == 1:
                return reply(act("click", ref=n["ref"], method="dom", expect="page moves on"))
            if fails == 2:
                return reply(act("click", ref=n["ref"], method="keyboard", expect="page moves on"))
    if tried(mem, r"\bwait\b") < 1:
        return reply(act("wait", expect="content loads"))
    if tried(mem, r"\bscroll\b") < 3:
        return reply(act("scroll", direction="down", expect="more of the page"))
    return reply(status="impossible", thought="no way forward found")


def _candidates(p: dict, want_parts: list[str]) -> list[dict]:
    out = []
    for n in p["nodes"]:
        if not n["ref"] or n["ref"] == p["target"] or n["role"] in ("textbox", "searchbox", "combobox", "heading"):
            continue
        label = n["name"] or n["text"]
        if n["section"] == "target" and n["role"] not in ("option", "listitem", "menuitem", "treeitem"):
            continue
        for depth, part in enumerate(want_parts):
            if label and matches_value(label, [part]) and len(label) < 80:
                out.append({**n, "depth": depth})
    out.sort(key=lambda n: -n["depth"])  # the deepest part of "Category > Choice" first
    return out


def decide_field(p: dict) -> dict:
    mem, value, target = p["memory"], p["value"], p["target"]
    parts = [x.strip() for x in value.split(">") if x.strip()] or [value]
    nodes = p["nodes"]
    tnode = next((n for n in nodes if n["ref"] == target), None)
    if tnode and tnode["role"] == "combobox" and "option" in [n["role"] for n in nodes if n["section"] == "target"] \
            and "[expanded]" not in tnode["attrs"] and tried(mem, r"select_option") < 1 and not tnode.get("text", "").startswith("Select..."):
        return reply(act("select_option", ref=target, option=parts[-1]))
    if tnode and tnode["role"] == "textbox" and tried(mem, r"\bfill\b") < 1:
        return reply(act("fill", ref=target, text=value))
    if tnode and tnode["role"] == "textbox":
        return reply(act("type_text", ref=target, text=value))
    if tnode and tnode["role"] == "checkbox":
        return reply(act("check", ref=target))
    cands = _candidates(p, parts)
    if cands:
        c = cands[0]
        label = (c["name"] or c["text"]).replace('"', "'")
        clicked = tried(mem, rf'click \w+ "{re.escape(label)}"')
        undone = tried(mem, r"NOT the approved value yet")
        if clicked < 1 or (clicked < 2 and undone and "only HIGHLIGHTED" not in mem):
            return reply(act("click", ref=c["ref"], expect=f"{label} chosen"))
        # Clicking only highlighted it (or it didn't stick): keyboard instead.
        return reply(act("press_key", key="Enter", expect="highlighted option chosen"))
    search = next((n for n in nodes if n["section"] == "popup" and n["role"] in ("searchbox", "textbox") and n["ref"]),
                  None)
    editable = tnode is not None and (tnode["role"] == "combobox" and "aria-autocomplete" not in tnode["attrs"]) \
        and "Select" not in (tnode.get("text") or "") and not tnode.get("text")
    term = parts[0] if tried(mem, r"type_text") < 1 else parts[0].split()[0][:4]
    typed = tried(mem, r"type_text")
    # A highlighted row exists after typing, but no clickable match: choose with the keyboard.
    if typed and tried(mem, r"ArrowDown") < 1 and tried(mem, r"wait") >= 1:
        return reply(act("press_key", key="ArrowDown"), act("press_key", key="Enter"))
    if typed and tried(mem, r"\bwait\b") < 1:
        return reply(act("wait", ms=2000, expect="search results"))
    if search and typed < 2:
        return reply(act("type_text", ref=search["ref"], text=term, expect="list filters"))
    if tnode and tnode["role"] == "combobox" and editable and typed < 2:
        return reply(act("type_text", ref=target, text=term, expect="suggestions appear"))
    if tnode and tnode["role"] == "combobox" and "[expanded]" not in tnode["attrs"]:
        clicks = tried(mem, r"click combobox")
        if clicks < 2:
            return reply(act("click", ref=target, expect="list opens"))
        if tried(mem, r"ArrowDown") < 1:
            return reply(act("press_key", ref=target, key="ArrowDown", expect="list opens"))
    if tnode and tnode["role"] == "generic" and tried(mem, r"click generic") < 2:
        return reply(act("click", ref=target, expect="list opens"))
    if tnode and tnode.get("box") and p["images"] and tried(mem, r"click_at") < 1:
        x, y, w, h = tnode["box"]
        idx = next((int(i) for i, kind in p["images"] if kind == "viewport"), 0)
        return reply(act("click_at", x=int(x + w / 2), y=int(y + h / 2), image=idx, expect="list opens"))
    if tried(mem, r"\bscroll\b") < 2:
        return reply(act("scroll", ref=target, direction="down", expect="more options"))
    return reply(status="impossible", thought="the wanted option isn't offered")


class HeuristicDecider:
    """Callable with the same signature as agent.ai_decider."""

    def __init__(self, fail_primary: int = 0) -> None:
        self.calls = 0
        self.fallback_calls = 0
        self.prompts: list[str] = []
        self.fail_primary = fail_primary  # primary model returns junk this many times

    async def __call__(self, system, prompt, images, prefer_fallback, job_id):
        self.calls += 1
        self.prompts.append(prompt)
        if prefer_fallback:
            self.fallback_calls += 1
        elif self.fail_primary > 0:
            self.fail_primary -= 1
            return reply(act("click", ref="e99999", expect="nonsense from a confused model")), "test", "primary", 1
        return decide(prompt), "test", "fallback" if prefer_fallback else "primary", 1


class ScriptedDecider:
    """Returns pre-written replies in order (for safety tests: a 'model' that misbehaves)."""

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []

    async def __call__(self, system, prompt, images, prefer_fallback, job_id):
        self.calls += 1
        self.prompts.append(prompt)
        r = self.replies.pop(0) if self.replies else reply(status="impossible", thought="script ended")
        return r, "test", "scripted", 1
