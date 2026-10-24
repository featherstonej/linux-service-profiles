"""State tracking for active profile and managed services."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field


class ManagedItem(BaseModel):
    """Represents a single service, container, stack, or tool managed by srvp."""

    provider: str = Field(
        ...,
        description="Provider key (e.g., systemd_system, systemd_user, docker_container, docker_compose, adb, esp32_serial).",
    )
    identifier: str = Field(..., description="Unique identifier within the provider.")
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Additional provider-specific attributes (e.g., compose file path).",
    )

    @property
    def key(self) -> tuple[str, str]:
        """Return a hashable (provider, identifier) key for set comparisons."""
        return (self.provider, self.identifier)


class AppState(BaseModel):
    """Persistent state stored in state.json."""

    active_profile: str | None = None
    activated_at: str | None = None
    active_items: list[ManagedItem] = Field(default_factory=list)


def resolve_state_path(custom_path: Path | str | None = None) -> Path:
    """Resolve the path to state.json using CLI flag, SRVP_STATE_PATH, or XDG default."""
    if custom_path is not None:
        return Path(custom_path).expanduser().resolve()
    env_path = os.environ.get("SRVP_STATE_PATH")
    if env_path:
        return Path(env_path).expanduser().resolve()
    xdg_state = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
    return (base / "linux-service-profiles" / "state.json").resolve()


class StateManager:
    """Reads and writes the active profile state file."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = resolve_state_path(path)

    def load(self) -> AppState:
        """Load state from disk, returning an empty AppState if not present or corrupted."""
        if not self.path.exists():
            return AppState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return AppState.model_validate(raw)
        except Exception:
            return AppState()

    def save(self, state: AppState) -> Path:
        """Persist state to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(state.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return self.path

    def record_activation(self, profile_name: str | None, items: list[ManagedItem]) -> AppState:
        """Update and save state for a newly activated profile (or None when stopped)."""
        now_iso = datetime.now(UTC).isoformat() if profile_name else None
        new_state = AppState(
            active_profile=profile_name,
            activated_at=now_iso,
            active_items=items,
        )
        self.save(new_state)
        return new_state
