"""Encrypted store for the job-site accounts JobPilot creates (Workday, iCIMS, ...).

- One account per site (the host name, e.g. acme.wd5.myworkdayjobs.com), always
  using the owner's job-search Gmail as the login.
- The owner chose ONE shared password for all job sites (generated once here, long
  and random). Trade-off accepted: a breach at one ATS exposes the password used on
  the others; these accounts only hold job applications.
- Passwords are encrypted with AES-256-GCM. The key lives only in .env (JOBPILOT_VAULT_KEY), never in the DB,
  so a copy of the database alone reveals nothing. Back up .env with the DB.
- Passwords are never sent to the AI, Telegram, events, or logs. The browser
  worker types them straight into the password fields the plan points at.
"""

import base64
import os
import secrets
import string

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import utcnow
from app.events import log_event
from app.models import SiteAccount

SYMBOLS = "!@*-_"  # accepted by Workday/iCIMS/Taleo rules; no % # $ (some older sites fail on them)


class VaultError(Exception):
    pass


def _key() -> bytes:
    raw = get_settings().vault_key
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as e:
        raise VaultError("JOBPILOT_VAULT_KEY is not valid base64") from e
    if len(key) != 32:
        raise VaultError("JOBPILOT_VAULT_KEY must be set (32 random bytes, base64)")
    return key


def encrypt(plaintext: str, site: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode(), site.encode())
    return base64.b64encode(nonce + ct).decode()


def decrypt(token: str, site: str) -> str:
    blob = base64.b64decode(token)
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], site.encode()).decode()


def generate_password(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                # At least one symbol every site counts as "special" (some don't count - or _).
                and any(c in "!@*" for c in pw)
                and pw[0].isalnum()):
            return pw


def site_key(url_or_host: str) -> str:
    from urllib.parse import urlsplit

    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def get_account(db: Session, site: str) -> SiteAccount | None:
    return db.scalar(select(SiteAccount).where(SiteAccount.site == site_key(site)))


SHARED_SITE = "*shared*"


def shared_password(db: Session) -> str:
    """The one job-site password, created on first use."""
    from app.settings_store import get_raw, put_raw

    stored = get_raw(db, "vault_shared_password")
    if stored and stored.get("enc"):
        return decrypt(stored["enc"], SHARED_SITE)
    pw = generate_password()
    put_raw(db, "vault_shared_password", {"enc": encrypt(pw, SHARED_SITE)})
    return pw


def get_or_create_account(db: Session, site: str, email: str, job_id: int | None = None) -> SiteAccount:
    site = site_key(site)
    acct = get_account(db, site)
    if acct:
        return acct
    if not email:
        raise VaultError("No job-search email set (JOBPILOT_GMAIL_ADDRESS)")
    acct = SiteAccount(site=site, login_email=email, password_enc=encrypt(shared_password(db), site),
                       status="creating")
    db.add(acct)
    db.flush()
    log_event(db, "system", "account.created", job_id=job_id, site=site, email=email)
    return acct


def security_answer(site: str, label: str) -> str:
    """Made-up answer to a job-site security question: the same site + question always gives
    the same answer (derived from the vault key, so nothing extra to store)."""
    import hashlib
    import hmac

    norm = " ".join((label or "").lower().split())
    d = hmac.new(_key(), f"security|{site_key(site)}|{norm}".encode(), hashlib.sha256).digest()
    # Readable but made up, e.g. "maple-harbor-42" (owner's rule: never real information).
    return f"{_WORDS[d[0] % len(_WORDS)]}-{_WORDS[d[1] % len(_WORDS)]}-{10 + d[2] % 90}"


_WORDS = ("maple", "harbor", "falcon", "cedar", "river", "copper", "meadow", "lantern", "orchid", "summit",
          "pebble", "willow", "comet", "thistle", "canyon", "juniper", "marble", "otter", "prairie", "quartz",
          "raven", "saffron", "tundra", "velvet", "walnut", "yarrow", "zephyr", "basil", "cobalt", "dune",
          "ember", "fjord", "garnet", "hazel", "indigo", "jasper", "kestrel", "lagoon", "mesa", "nutmeg",
          "onyx", "pine", "quill", "russet", "sparrow", "tulip", "umber", "violet", "wren", "aspen",
          "birch", "clover", "delta", "elm", "fern", "glacier", "heron", "iris", "juno", "kelp",
          "lilac", "moss", "nova", "opal")


def password_of(acct: SiteAccount) -> str:
    return decrypt(acct.password_enc, acct.site)


def mark(db: Session, acct: SiteAccount, status: str) -> None:
    acct.status = status
    acct.last_used_at = utcnow()
