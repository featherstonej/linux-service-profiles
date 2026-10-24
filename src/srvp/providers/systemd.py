"""Systemd system and user unit providers with batched sudo support."""

from __future__ import annotations

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


def _parse_systemctl_show(output: str) -> tuple[bool, int | None]:
    """Parse `systemctl show --property=ActiveState,MainPID` output."""
    active = False
    pid: int | None = None
    for line in output.splitlines():
        if line.startswith("ActiveState="):
            val = line.split("=", 1)[1].strip()
            active = val == "active"
        elif line.startswith("MainPID="):
            try:
                parsed_pid = int(line.split("=", 1)[1].strip())
                if parsed_pid > 0:
                    pid = parsed_pid
            except ValueError:
                pass
    return active, pid


class SystemdSystemProvider(ServiceProvider):
    """Manages system-level systemd units via batched `sudo systemctl` commands."""

    provider_name = "systemd_system"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        return [
            ManagedItem(provider=self.provider_name, identifier=unit)
            for unit in profile.effective_system_units()
        ]

    def get_explicit_stop_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        return [
            ManagedItem(provider=self.provider_name, identifier=unit)
            for unit in profile.effective_ensure_stopped_units()
        ]

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        res = executor.run(
            ["systemctl", "show", item.identifier, "--property=ActiveState,MainPID"],
            sudo=False,
            mutating=False,
        )
        active, pid = _parse_systemctl_show(res.stdout)
        metrics = executor.get_process_metrics(pid) if (active and pid) else None
        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=active,
            status_text="active" if active else "inactive",
            pid=pid,
            cpu_percent=metrics.cpu_percent if metrics else 0.0,
            memory_mb=metrics.memory_mb if metrics else 0.0,
            detail="system unit",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        if not items:
            return []
        unit_names = [it.identifier for it in items]
        res = executor.run(["systemctl", "start", *unit_names], sudo=True, mutating=True)
        return [
            ActionRecord(
                action="start",
                provider=self.provider_name,
                identifier=u,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for u in unit_names
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        unit_names = [it.identifier for it in items]
        res = executor.run(["systemctl", "stop", *unit_names], sudo=True, mutating=True)
        return [
            ActionRecord(
                action="stop",
                provider=self.provider_name,
                identifier=u,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for u in unit_names
        ]


class SystemdUserProvider(ServiceProvider):
    """Manages user-level systemd units via `systemctl --user`."""

    provider_name = "systemd_user"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        return [
            ManagedItem(provider=self.provider_name, identifier=unit)
            for unit in profile.systemd.user
        ]

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        res = executor.run(
            ["systemctl", "--user", "show", item.identifier, "--property=ActiveState,MainPID"],
            sudo=False,
            mutating=False,
        )
        active, pid = _parse_systemctl_show(res.stdout)
        metrics = executor.get_process_metrics(pid) if (active and pid) else None
        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=active,
            status_text="active" if active else "inactive",
            pid=pid,
            cpu_percent=metrics.cpu_percent if metrics else 0.0,
            memory_mb=metrics.memory_mb if metrics else 0.0,
            detail="user unit",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        if not items:
            return []
        unit_names = [it.identifier for it in items]
        res = executor.run(["systemctl", "--user", "start", *unit_names], sudo=False, mutating=True)
        return [
            ActionRecord(
                action="start",
                provider=self.provider_name,
                identifier=u,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for u in unit_names
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        unit_names = [it.identifier for it in items]
        res = executor.run(["systemctl", "--user", "stop", *unit_names], sudo=False, mutating=True)
        return [
            ActionRecord(
                action="stop",
                provider=self.provider_name,
                identifier=u,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for u in unit_names
        ]
