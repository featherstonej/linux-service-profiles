"""Base classes and data structures for srvp service providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.state import ManagedItem


@dataclass(frozen=True)
class ServiceStatus:
    """Live runtime status and resource telemetry for a managed item."""

    provider: str
    identifier: str
    active: bool
    status_text: str
    pid: int | None = None
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class ActionRecord:
    """Record of a single start/stop/hook operation performed during a transition."""

    action: str  # "start", "stop", "hook", "check"
    provider: str
    identifier: str
    command: str
    success: bool
    message: str = ""
    dry_run: bool = False


class ServiceProvider(ABC):
    """Abstract base class for a pluggable service or tool provider."""

    provider_name: str

    @abstractmethod
    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        """Return items that should be active when `profile` is active."""

    def get_explicit_stop_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        """Return items that `profile` explicitly requires to be stopped."""
        return []

    @abstractmethod
    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        """Query the live status and PID metrics of `item`."""

    @abstractmethod
    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        """Start the specified items, batching commands when possible."""

    @abstractmethod
    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        """Stop the specified items, batching commands when possible."""
