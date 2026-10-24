"""State tracking for active profile and managed services."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

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

    @property
    def resource_key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        """Return a hashable (provider, identifier, metadata) key to distinguish modified resources."""
        return (self.provider, self.identifier, tuple(sorted(self.metadata.items())))


class AppState(BaseModel):
    """Persistent state stored in state.json."""

    active_profile: str | None = None
    activated_at: str | None = None
    active_items: list[ManagedItem] = Field(default_factory=list)
    pre_stop_hooks: list[str] = Field(default_factory=list)
    post_stop_hooks: list[str] = Field(default_factory=list)


def resolve_state_path(custom_path: Path | str | None = None, *, mock: bool = False) -> Path:
    """Resolve the path to state.json using CLI flag, SRVP_STATE_PATH, or XDG default.

    When `mock` or `SRVP_MOCK=1` is enabled, isolates activation state into a `mock_`-prefixed
    state file so mock runs never overwrite or consume real host activation state.
    """
    if custom_path is not None:
        resolved = Path(custom_path).expanduser().resolve()
    else:
        env_path = os.environ.get("SRVP_STATE_PATH")
        if env_path:
            resolved = Path(env_path).expanduser().resolve()
        else:
            xdg_state = os.environ.get("XDG_STATE_HOME")
            base = Path(xdg_state).expanduser() if xdg_state else Path.home() / ".local" / "state"
            resolved = (base / "linux-service-profiles" / "state.json").resolve()

    if (mock or os.environ.get("SRVP_MOCK") == "1") and not resolved.name.startswith("mock_"):
        resolved = resolved.with_name(f"mock_{resolved.name}")
    return resolved


class StateManager:
    """Reads and writes the active profile state file."""

    def __init__(self, path: Path | str | None = None, *, mock: bool = False) -> None:
        if path is not None and not mock and os.environ.get("SRVP_MOCK") != "1":
            self.path = Path(path).expanduser().resolve()
        else:
            self.path = resolve_state_path(path, mock=mock)
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_handle: TextIO | None = None

    @property
    def lock_path(self) -> Path:
        """Path to the interprocess lock file guarding transitions on this state file."""
        return self.path.with_name(f"{self.path.name}.lock")

    @contextmanager
    def transition_lock(self) -> Iterator[None]:
        """Acquire a re-entrant thread and interprocess exclusive lock across a transition."""
        with self._thread_lock:
            if self._lock_depth == 0:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self.lock_path.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except Exception:
                    handle.close()
                    raise
                self._lock_handle = handle
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if self._lock_depth == 0 and self._lock_handle is not None:
                    try:
                        fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        self._lock_handle.close()
                        self._lock_handle = None

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

    def record_activation(
        self,
        profile_name: str | None,
        items: list[ManagedItem],
        *,
        pre_stop_hooks: list[str] | None = None,
        post_stop_hooks: list[str] | None = None,
    ) -> AppState:
        """Update and save state for a newly activated profile (or None when stopped)."""
        now_iso = datetime.now(UTC).isoformat() if profile_name else None
        new_state = AppState(
            active_profile=profile_name,
            activated_at=now_iso,
            active_items=items,
            pre_stop_hooks=list(pre_stop_hooks or []) if profile_name else [],
            post_stop_hooks=list(post_stop_hooks or []) if profile_name else [],
        )
        self.save(new_state)
        return new_state

    def rename_active_profile(self, old_name: str, new_name: str) -> AppState:
        """Migrate active_profile when the currently active profile is renamed."""
        state = self.load()
        if state.active_profile == old_name:
            state.active_profile = new_name
            self.save(state)
        return state
