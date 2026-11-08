"""Owner-editable settings kept in the database, with typed defaults."""

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.models import SettingKV

ProviderName = Literal["claude", "codex", "gemini", "none"]
# Reasoning effort. "" = the model's default. Claude CLI: --effort (low..max);
# Codex CLI: -c model_reasoning_effort=... (per-model list, some add "ultra").
EffortName = Literal["", "low", "medium", "high", "xhigh", "max", "ultra"]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

# Tasks that may use AI (SPEC.md section 5). Each can have its own provider+model.
AI_TASKS: dict[str, str] = {
    "form_mapper": "Read application forms and map fields to profile answers",
    "job_fit": "Read job postings and judge fit against the profile",
    "email_classifier": "Classify unclear employer emails",
    "chat": "Chat assistant",
}

# Model choices per provider for the Settings dropdowns. "" means "the CLI's default".
# Defaults below; scripts/refresh-models.sh writes <data_dir>/models.json from the
# installed CLIs' own model lists, which overrides these when present.
DEFAULT_MODEL_CHOICES: dict[str, list[str]] = {
    # Aliases always point at the latest model of that tier (per `claude --help`).
    "claude": ["", "haiku", "sonnet", "opus", "fable"],
    "codex": [""],
    "gemini": [""],
    "none": [],
}


def _models_json() -> dict:
    import json

    from app.config import get_settings

    try:
        data = json.loads((get_settings().data_dir / "models.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


_EFFORT_SET = {"low", "medium", "high", "xhigh", "max", "ultra"}


def effort_choices() -> dict[str, dict[str, list[str]]]:
    """Effort levels per provider and model. Key "*" = levels for any model of that provider."""
    out: dict[str, dict[str, list[str]]] = {"claude": {"*": list(CLAUDE_EFFORTS)}, "codex": {}, "gemini": {}}
    efforts = _models_json().get("efforts")
    if isinstance(efforts, dict):
        for provider in ("claude", "codex", "gemini"):
            per = efforts.get(provider)
            if not isinstance(per, dict):
                continue
            for model, levels in per.items():
                if isinstance(model, str) and isinstance(levels, list):
                    out[provider][model] = [x for x in levels if x in _EFFORT_SET]
    return out


def model_choices() -> dict[str, list[str]]:
    """Dropdown choices: defaults, overridden per provider by models.json if valid."""
    out = {k: list(v) for k, v in DEFAULT_MODEL_CHOICES.items()}
    data = _models_json()
    for provider in ("claude", "codex", "gemini"):
        models = data.get(provider)
        if isinstance(models, list) and all(isinstance(m, str) and len(m) < 100 for m in models):
            out[provider] = [""] + [m for m in models if m]
    return out


class TaskRoute(BaseModel):
    provider: ProviderName = "claude"
    model: str = "haiku"
    fallback_provider: ProviderName = "none"
    fallback_model: str = ""
    effort: EffortName = ""
    fallback_effort: EffortName = ""


class AISettings(BaseModel):
    tasks: dict[str, TaskRoute] = Field(
        default_factory=lambda: {
            "form_mapper": TaskRoute(provider="claude", model="sonnet"),
            "job_fit": TaskRoute(provider="claude", model="sonnet"),
            "email_classifier": TaskRoute(provider="claude", model="haiku"),
            "chat": TaskRoute(provider="claude", model="sonnet"),
        }
    )


class AutomationSettings(BaseModel):
    # Emergency stop. When true, no automated browser/AI work starts.
    paused: bool = True
    # dry_run: fill forms but never click final submit.
    dry_run: bool = True
    # auto_submit: press Submit without waiting for a Telegram tap (a screenshot is still sent).
    auto_submit: bool = True
    daily_summary_hour: int = Field(default=20, ge=0, le=23)
    # Before applying, compare the posting with the resume; skip if below min_fit_score.
    fit_check: bool = True
    min_fit_score: int = Field(default=50, ge=0, le=100)


_DEFAULTS: dict[str, type[BaseModel]] = {
    "ai": AISettings,
    "automation": AutomationSettings,
}


def get_setting[T: BaseModel](db: Session, key: str, model: type[T]) -> T:
    row = db.get(SettingKV, key)
    if row is None:
        return model()
    return model.model_validate(row.value)


def put_setting(db: Session, key: str, value: BaseModel) -> None:
    data: dict[str, Any] = value.model_dump(mode="json")
    row = db.get(SettingKV, key)
    if row is None:
        db.add(SettingKV(key=key, value=data))
    else:
        row.value = data


def get_raw(db: Session, key: str) -> dict | None:
    row = db.get(SettingKV, key)
    return None if row is None else row.value


def put_raw(db: Session, key: str, value: dict) -> None:
    row = db.get(SettingKV, key)
    if row is None:
        db.add(SettingKV(key=key, value=value))
    else:
        row.value = value
