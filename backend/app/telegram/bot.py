"""Telegram bot: notifications, owner questions, and quick commands.

Security:
- Only the configured owner's Telegram user ID is obeyed; everyone else is ignored.
- Uses long polling (getUpdates), so no inbound port or public webhook is needed.
- Messages are sent as plain text (no Markdown/HTML parsing) so job/employer text
  can never inject formatting or links.
- Telegram bot chats are NOT end-to-end encrypted: never send secrets here.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import session_factory
from app.events import log_event
from app.models import OwnerQuestion
from app.profile import ProfileError
from app.questions import answer_question, latest_open
from app.settings_store import AutomationSettings, get_raw, get_setting, put_raw, put_setting

log = logging.getLogger("jobpilot.telegram")

HELP_TEXT = (
    "JobPilot commands:\n"
    "/status - what's happening right now\n"
    "/questions - questions waiting for your answer\n"
    "/setup - answer the common job-application questions once, up front\n"
    "/pause - emergency stop: no new automated work starts\n"
    "/unpause - allow automated work again\n"
    "/help - this message\n\n"
    "To answer a question, tap a button or reply to the question message. "
    "Never send passwords, SSNs or bank details here."
)

_status: dict[str, Any] = {"running": False, "last_ok": None, "last_error": None}


def bot_status() -> dict[str, Any]:
    return dict(_status)


class TelegramAPI:
    def __init__(self, token: str, base: str, client: httpx.AsyncClient) -> None:
        self._url = f"{base}/bot{token}"
        self._client = client

    async def call(self, method: str, **params: Any) -> Any:
        r = await self._client.post(f"{self._url}/{method}", json=params, timeout=40)
        body = r.json()
        if not body.get("ok"):
            # Never include the URL (it contains the token) in errors.
            raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
        return body["result"]

    async def send_photo(
        self, chat_id: int, path: str, caption: str, buttons: list[list[dict]] | None = None
    ) -> dict:
        """Send a screenshot. Tall full-page shots are rejected as photos, so fall back
        to sending them as a document (still viewable in Telegram)."""
        import json as _json
        from pathlib import Path as _Path

        data: dict[str, Any] = {"chat_id": str(chat_id), "caption": caption[:1000]}
        if buttons:
            data["reply_markup"] = _json.dumps({"inline_keyboard": buttons})
        raw = _Path(path).read_bytes()
        for method, field in (("sendPhoto", "photo"), ("sendDocument", "document")):
            r = await self._client.post(
                f"{self._url}/{method}", data=data, files={field: (_Path(path).name, raw, "image/png")},
                timeout=60,
            )
            body = r.json()
            if body.get("ok"):
                return body["result"]
        raise RuntimeError(f"Telegram photo failed: {body.get('description')}")

    async def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text[:4000]}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return await self.call("sendMessage", **params)


def format_status(summary: dict) -> str:
    s = summary
    by = s["jobs_by_status"]
    lines = [
        "JobPilot status",
        f"Automation: {'PAUSED' if s['automation_paused'] else 'running'}"
        + (" (dry run)" if s["dry_run"] else ""),
        f"Last {s['window_hours']}h: {s['new_jobs_in_window']} new jobs tracked",
        f"Tracked jobs: {s['jobs_total']}",
        f"  submitted (confirmed): {by['submitted_confirmed']}",
        f"  submitted (uncertain): {by['submitted_uncertain']}",
        f"  interviews: {by['interview']}",
        f"Waiting on you: {s['waiting_for_owner']}",
    ]
    if s["unconfirmed_facts"]:
        lines.append(f"Profile facts to confirm: {s['unconfirmed_facts']}")
    if s["active_resume_version"] is None:
        lines.append("No resume uploaded yet.")
    return "\n".join(lines)


class Bot:
    """Pure update-handling logic; network I/O goes through TelegramAPI."""

    def __init__(self, api: TelegramAPI, owner_id: int | None, db_factory=session_factory) -> None:
        self.api = api
        self.owner_id = owner_id
        self.db_factory = db_factory
        self._tasks: set[asyncio.Task] = set()
        self._reading: set[int] = set()  # info questions whose reply is being read now

    def _db(self) -> Session:
        return self.db_factory()()

    async def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    async def _handle_message(self, msg: dict) -> None:
        sender = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if self.owner_id is None:
            # Setup helper: tell whoever messages the bot their own ID. Reveals nothing.
            await self.api.send(
                chat_id,
                f"Your Telegram user ID is {sender}. Put it in JOBPILOT_TELEGRAM_OWNER_ID "
                "in JobPilot's .env file and restart JobPilot.",
            )
            return
        if sender != self.owner_id or chat_id != self.owner_id:
            with self._db() as db:
                log_event(db, "telegram", "telegram.ignored_sender", sender_id=sender)
                db.commit()
            return
        if not text:
            return

        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
        if cmd in ("/start", "/help"):
            await self.api.send(chat_id, HELP_TEXT)
        elif cmd == "/status":
            await self.api.send(chat_id, self._status_text())
        elif cmd in ("/pause", "/stop"):
            self._set_paused(True)
            await self.api.send(chat_id, "Paused. No new automated work will start. /unpause to resume.")
        elif cmd == "/unpause":
            self._set_paused(False)
            await self.api.send(chat_id, "Unpaused. Automated work may start again.")
        elif cmd == "/questions":
            await self._send_open_questions(chat_id)
        elif cmd == "/setup":
            await self._send_setup(chat_id)
        elif cmd:
            await self.api.send(chat_id, "Unknown command. /help for the list.")
        else:
            await self._answer_free_text(chat_id, text, msg.get("reply_to_message"))

    async def _handle_callback(self, cq: dict) -> None:
        sender = (cq.get("from") or {}).get("id")
        if self.owner_id is None or sender != self.owner_id:
            return
        await self.api.call("answerCallbackQuery", callback_query_id=cq["id"])
        data = cq.get("data") or ""
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "q" and parts[1].isdigit() and parts[2].isdigit():
            with self._db() as db:
                q = db.get(OwnerQuestion, int(parts[1]))
                idx = int(parts[2])
                if q is None or q.status != "open" or idx >= len(q.options):
                    await self.api.send(sender, "That question was already answered or closed.")
                    return
                await self._save_answer(db, sender, q, q.options[idx])

    async def _answer_free_text(self, chat_id: int, text: str, reply_to: dict | None) -> None:
        with self._db() as db:
            q: OwnerQuestion | None = None
            if reply_to:
                q = db.query(OwnerQuestion).filter(
                    OwnerQuestion.telegram_message_id == reply_to.get("message_id")
                ).one_or_none()
            if q is None:
                q = latest_open(db)
            if q is None or q.status != "open":
                await self.api.send(chat_id, "No open question to answer. /help for commands.")
                return
            await self._save_answer(db, chat_id, q, text)

    async def _save_answer(self, db: Session, chat_id: int, q: OwnerQuestion, answer: str) -> None:
        if q.kind == "info":
            if q.id in self._reading:
                await self.api.send(chat_id, "Still reading your last answer, one moment.")
                return
            self._reading.add(q.id)
            await self.api.send(chat_id, "Got it. Reading your answer...")
            t = asyncio.create_task(self._read_info(chat_id, q.id, answer))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
            return
        try:
            answer_question(db, q, answer, actor="telegram")
            db.commit()
        except (ProfileError, ValueError) as e:
            db.rollback()
            await self.api.send(chat_id, f"Not saved: {e}")
            return
        if q.kind == "takeover" and answer not in q.options:
            await self.api.send(chat_id, "Got it. Trying again with your instructions.")
            return
        if q.kind != "fact":
            await self.api.send(chat_id, f"Got it: {answer}")
            return
        await self.api.send(
            chat_id, f'Saved to your profile:\n"{q.question}"\n-> {answer}\n'
            "You can edit it anytime on the About Me page."
        )

    async def _read_info(self, chat_id: int, qid: int, answer: str) -> None:
        """Turn a free-text reply to a list of questions into facts, then close the question
        (the apply worker re-reads the page once it's closed)."""
        from app.info_answers import AIUnavailable, absorb, summary_text

        try:
            with self._db() as db:
                q = db.get(OwnerQuestion, qid)
                if q is None or q.status != "open":
                    return
                question, job_id, setup = q.question, q.job_id, q.section == "setup"
            try:
                result = await absorb(question, answer, job_id)
                text = summary_text(result)
            except AIUnavailable as e:
                log.warning("reading answer failed: %s", str(e)[:200])
                text = "I couldn't read that right now (AI unavailable). I'll ask again."
            with self._db() as db:
                q = db.get(OwnerQuestion, qid)
                if q is not None and q.status == "open":
                    answer_question(db, q, answer, actor="telegram")
                    db.commit()
            await self.api.send(chat_id, text)
            if setup:
                await self._send_setup(chat_id, after=True)
        except Exception as e:  # noqa: BLE001
            log.warning("info answer failed: %s", type(e).__name__)
        finally:
            self._reading.discard(qid)

    async def _send_setup(self, chat_id: int, after: bool = False) -> None:
        """Ask the next group of unanswered common application questions in one message."""
        from app.common_info import missing
        from app.questions import create_question

        with self._db() as db:
            for old in db.query(OwnerQuestion).filter(OwnerQuestion.status == "open",
                                                      OwnerQuestion.section == "setup").all():
                old.status = "expired"
            todo = missing(db)
            if not todo:
                db.commit()
                await self.api.send(chat_id, "All the common application questions are answered."
                                    if after else "You've already answered all the common questions. "
                                    "Change any of them on the About Me page.")
                return
            group = todo[0].group
            items = [q for q in todo if q.group == group]
            lines = [f"Setup: {group} ({len(todo)} common questions left)",
                     "Answer in one message, however you like. Skip any you want.", ""]
            for i, q in enumerate(items, 1):
                extra = f" ({' / '.join(q.options)})" if q.options else (f" ({q.help})" if q.help else "")
                lines.append(f"{i}. {q.label}{extra}")
            lines += ["", "Easier with buttons: About Me page -> Common questions."]
            q = create_question(db, "\n".join(lines), [], job_id=None, section="setup", kind="info")
            db.commit()
            await self.send_question(db, q, chat_id)
            db.commit()

    async def _send_open_questions(self, chat_id: int) -> None:
        with self._db() as db:
            qs = db.query(OwnerQuestion).filter(OwnerQuestion.status == "open").all()
            if not qs:
                await self.api.send(chat_id, "No questions waiting. Nice.")
                return
            for q in qs:
                await self.send_question(db, q, chat_id)
            db.commit()

    async def send_question(self, db: Session, q: OwnerQuestion, chat_id: int | None = None) -> None:
        chat_id = chat_id or self.owner_id
        if chat_id is None:
            return
        buttons = [[{"text": o, "callback_data": f"q:{q.id}:{i}"}] for i, o in enumerate(q.options)]
        if q.kind == "fact":
            from app.models import Job

            job = db.get(Job, q.job_id) if q.job_id else None
            where = f" ({job.employer})" if job and job.employer else ""
            text = f"Question from JobPilot{where}:\n{q.question}"
            hint = "Tap an option, or reply to this message." if buttons else "Reply to this message."
            text += "\n\n" + hint
        else:
            text = q.question
        sent: dict | None = None
        if q.photo_path:
            path = get_settings().data_dir / q.photo_path
            try:
                sent = await self.api.send_photo(chat_id, str(path), text, buttons or None)
            except Exception as e:  # noqa: BLE001  (screenshot is a nice-to-have)
                msg = str(e).replace(get_settings().telegram_bot_token or "\0", "***")
                log.warning("Could not send screenshot: %s", msg[:200])
        if sent is None:
            sent = await self.api.send(chat_id, text, buttons or None)
        q.telegram_message_id = sent.get("message_id")

    def _status_text(self) -> str:
        from app.api.overview_routes import summary

        with self._db() as db:
            return format_status(summary(db, 24))

    def _set_paused(self, paused: bool) -> None:
        with self._db() as db:
            a = get_setting(db, "automation", AutomationSettings)
            a.paused = paused
            put_setting(db, "automation", a)
            log_event(db, "telegram", "settings.paused" if paused else "settings.unpaused")
            db.commit()

    async def maybe_send_daily_summary(self, now: datetime | None = None) -> bool:
        if self.owner_id is None:
            return False
        tz = ZoneInfo(get_settings().timezone)
        now = now or datetime.now(tz)
        with self._db() as db:
            hour = get_setting(db, "automation", AutomationSettings).daily_summary_hour
            state = get_raw(db, "telegram_state") or {}
            today = now.date().isoformat()
            if now.hour != hour or state.get("last_summary_date") == today:
                return False
            state["last_summary_date"] = today
            put_raw(db, "telegram_state", state)
            db.commit()
        await self.api.send(self.owner_id, "Daily summary\n" + self._status_text())
        return True

    async def push_open_questions(self) -> None:
        """Send any open questions that haven't been sent to Telegram yet."""
        if self.owner_id is None:
            return
        with self._db() as db:
            qs = db.query(OwnerQuestion).filter(
                OwnerQuestion.status == "open", OwnerQuestion.telegram_message_id.is_(None)
            ).all()
            for q in qs:
                await self.send_question(db, q)
            db.commit()


_notify_queue: "asyncio.Queue[tuple[str, str | None]]" = asyncio.Queue(maxsize=50)


def notify(text: str, photo_path: str | None = None) -> None:
    """Queue a one-way message to the owner (sent by the bot loop). Never raises."""
    try:
        _notify_queue.put_nowait((text, photo_path))
    except asyncio.QueueFull:
        log.warning("Telegram notify queue full; dropping message")


async def _flush_notifications(api: "TelegramAPI", owner_id: int | None) -> None:
    while not _notify_queue.empty():
        text, photo = _notify_queue.get_nowait()
        if owner_id is None:
            continue
        try:
            if photo:
                await api.send_photo(owner_id, str(get_settings().data_dir / photo), text)
            else:
                await api.send(owner_id, text)
        except Exception as e:  # noqa: BLE001
            msg = str(e).replace(get_settings().telegram_bot_token or "\0", "***")
            log.warning("Telegram notify failed: %s", msg[:200])


async def run_bot() -> None:
    """Long-poll loop. Started from the app lifespan when a token is configured."""
    s = get_settings()
    if not s.telegram_bot_token:
        log.info("Telegram disabled: no token configured")
        return
    async with httpx.AsyncClient() as client:
        api = TelegramAPI(s.telegram_bot_token, s.telegram_api_base, client)
        bot = Bot(api, s.telegram_owner_id)
        _status["running"] = True
        offset: int | None = None
        backoff = 2
        while True:
            try:
                await bot.maybe_send_daily_summary()
                await bot.push_open_questions()
                await _flush_notifications(api, bot.owner_id)
                params: dict[str, Any] = {"timeout": 8, "allowed_updates": ["message", "callback_query"]}
                if offset is not None:
                    params["offset"] = offset
                updates = await api.call("getUpdates", **params)
                for u in updates:
                    offset = u["update_id"] + 1
                    try:
                        await bot.handle_update(u)
                    except Exception:  # one bad update must not kill the loop
                        log.exception("Failed handling Telegram update")
                _status["last_ok"] = datetime.now().isoformat(timespec="seconds")
                _status["last_error"] = None
                backoff = 2
            except asyncio.CancelledError:
                _status["running"] = False
                raise
            except Exception as e:  # noqa: BLE001
                _status["last_error"] = str(e).replace(s.telegram_bot_token, "***")[:300]
                log.warning("Telegram poll error: %s", _status["last_error"])
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
