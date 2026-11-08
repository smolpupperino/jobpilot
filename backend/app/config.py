"""Runtime configuration, read from environment variables (see .env.example).

Secrets (session key, Telegram token) come from the environment or files and are
never written to the database or logs.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: a blank line like `JOBPILOT_TELEGRAM_OWNER_ID=` means "not set".
    model_config = SettingsConfigDict(env_prefix="JOBPILOT_", env_file=None, env_ignore_empty=True)

    data_dir: Path = Path("/data")
    timezone: str = "America/Los_Angeles"

    # Random secret used to sign session cookies. Required in production.
    session_secret: str = Field(default="", repr=False)
    session_max_age_hours: int = 72
    # Set to False only for local http testing; Tailscale serve gives https.
    cookie_secure: bool = True

    telegram_bot_token: str = Field(default="", repr=False)
    telegram_owner_id: int | None = None
    telegram_api_base: str = "https://api.telegram.org"

    # Passwordless login via Tailscale identity. Tailscale Serve adds a
    # Tailscale-User-Login header (and strips any copy the client sent). If it
    # matches this login, the request counts as the owner. Blank = off.
    tailscale_owner_login: str = ""

    # Browser automation (Phase 4). The browser container's DevTools relay and the
    # host-side AI runner socket. browser_view_url is the noVNC link sent to the
    # owner when they need to watch or take over.
    browser_cdp_host: str = "browser"
    browser_cdp_port: int = 9223
    browser_view_url: str = ""
    ai_socket: Path = Path("/run/jobpilot-ai/ai.sock")
    # Job-site account vault key (32 random bytes, base64). Generated into .env.
    vault_key: str = Field(default="", repr=False)
    # The dedicated job-search Gmail: login for job-site accounts, and where
    # verification codes/links arrive. App password = Google "App passwords".
    gmail_address: str = ""
    gmail_app_password: str = Field(default="", repr=False)
    gmail_imap_host: str = "imap.gmail.com"
    # Tests/dev only: read verification emails from .eml files in this folder instead of IMAP.
    dev_mailbox_dir: str = ""

    # How long a run waits for the owner (question/approval/takeover) before giving up.
    owner_wait_minutes: int = 60

    # Public-facing dashboard base URL (e.g. https://mini.tailnet.ts.net) used in
    # Telegram links. Optional.
    public_base_url: str = ""

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobpilot.db"

    @property
    def resume_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def upload_dir(self) -> Path:
        """Nicely-named copies of the resume for uploads (also mounted into the browser)."""
        return self.data_dir / "upload"


@lru_cache
def get_settings() -> Settings:
    return Settings()
