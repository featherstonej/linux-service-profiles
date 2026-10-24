"""Systemd system and user unit providers with batched sudo support."""

from __future__ import annotations

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


def _is_unit_confirmed_not_found(
    unit_name: str,
    executor: CommandExecutor,
    *,
    user: bool = False,
) -> bool:
    """Return True if systemd confirms the unit is not-found and inactive."""
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(["show", unit_name, "--property=LoadState,ActiveState"])
    res = executor.run(cmd, sudo=False, mutating=False)
    if res.returncode != 0:
        return False
    load_state = ""
    active_state = ""
    for line in res.stdout.splitlines():
        if line.startswith("LoadState="):
            load_state = line.split("=", 1)[1].strip()
        elif line.startswith("ActiveState="):
            active_state = line.split("=", 1)[1].strip()
    return load_state == "not-found" and active_state in ("inactive", "dead", "")


def _is_unit_activating(
    unit_name: str,
    executor: CommandExecutor,
    *,
    user: bool = False,
) -> bool:
    """Return True if systemd reports that a unit is still starting."""
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(["show", unit_name, "--property=ActiveState"])
    res = executor.run(cmd, sudo=False, mutating=False)
    return res.returncode == 0 and any(
        line.strip() == "ActiveState=activating" for line in res.stdout.splitlines()
    )


def _parse_systemctl_show(output: str) -> tuple[bool, int | None]:
    """Parse `systemctl show --property=ActiveState,MainPID` output."""
    active = False
    pid: int | None = None
    for line in output.splitlines():
        if line.startswith("ActiveState="):
            val = line.split("=", 1)[1].strip()
            active = val in ("active", "activating", "reloading")
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

    def is_start_pending(self, item: ManagedItem, executor: CommandExecutor) -> bool:
        return _is_unit_activating(item.identifier, executor)

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
        if res.returncode == 0 or res.dry_run:
            outcomes = [True] * len(items)
        elif len(items) > 1:
            active_flags = [self.get_status(it, executor).active for it in items]
            outcomes = (
                active_flags
                if (any(active_flags) and not all(active_flags))
                else [False] * len(items)
            )
        else:
            outcomes = [False]
        return [
            ActionRecord(
                action="start",
                provider=self.provider_name,
                identifier=u,
                command=res.command_str,
                success=ok,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for u, ok in zip(unit_names, outcomes, strict=True)
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        present_items: list[ManagedItem] = []
        absent_records: dict[str, ActionRecord] = {}
        if not executor.is_dry_run:
            for it in items:
                if _is_unit_confirmed_not_found(it.identifier, executor, user=False):
                    absent_records[it.identifier] = ActionRecord(
                        action="stop",
                        provider=self.provider_name,
                        identifier=it.identifier,
                        command=f"systemctl stop {it.identifier} (not-found)",
                        success=True,
                        message="Unit not installed (not-found); already inactive.",
                        dry_run=False,
                    )
                else:
                    present_items.append(it)
        else:
            present_items = list(items)

        batch_records: dict[str, ActionRecord] = {}
        if present_items:
            unit_names = [it.identifier for it in present_items]
            extra_socket = (
                "docker.service" in unit_names
                and "docker.socket" not in unit_names
                and (
                    executor.is_dry_run
                    or not _is_unit_confirmed_not_found("docker.socket", executor, user=False)
                )
            )
            cmd_units = [*unit_names, "docker.socket"] if extra_socket else unit_names
            res = executor.run(["systemctl", "stop", *cmd_units], sudo=True, mutating=True)
            if res.returncode == 0 or res.dry_run:
                outcomes = [True] * len(present_items)
            elif len(present_items) > 1:
                active_flags = [self.get_status(it, executor).active for it in present_items]
                outcomes = (
                    [not flag for flag in active_flags]
                    if (any(active_flags) and not all(active_flags))
                    else [False] * len(present_items)
                )
            else:
                outcomes = [False]
            for u, ok in zip(unit_names, outcomes, strict=True):
                batch_records[u] = ActionRecord(
                    action="stop",
                    provider=self.provider_name,
                    identifier=u,
                    command=res.command_str,
                    success=ok,
                    message=res.stdout or res.stderr,
                    dry_run=res.dry_run,
                )

        return [
            absent_records[it.identifier]
            if it.identifier in absent_records
            else batch_records[it.identifier]
            for it in items
        ]


class SystemdUserProvider(ServiceProvider):
    """Manages user-level systemd units via `systemctl --user`."""

    provider_name = "systemd_user"

    def is_start_pending(self, item: ManagedItem, executor: CommandExecutor) -> bool:
        return _is_unit_activating(item.identifier, executor, user=True)

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
        if res.returncode == 0 or res.dry_run:
            outcomes = [True] * len(items)
        elif len(items) > 1:
            active_flags = [self.get_status(it, executor).active for it in items]
            outcomes = (
                active_flags
                if (any(active_flags) and not all(active_flags))
                else [False] * len(items)
            )
        else:
            outcomes = [False]
        return [
            ActionRecord(
                action="start",
                provider=self.provider_name,
                identifier=u,
                command=res.command_str,
                success=ok,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for u, ok in zip(unit_names, outcomes, strict=True)
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        present_items: list[ManagedItem] = []
        absent_records: dict[str, ActionRecord] = {}
        if not executor.is_dry_run:
            for it in items:
                if _is_unit_confirmed_not_found(it.identifier, executor, user=True):
                    absent_records[it.identifier] = ActionRecord(
                        action="stop",
                        provider=self.provider_name,
                        identifier=it.identifier,
                        command=f"systemctl --user stop {it.identifier} (not-found)",
                        success=True,
                        message="Unit not installed (not-found); already inactive.",
                        dry_run=False,
                    )
                else:
                    present_items.append(it)
        else:
            present_items = list(items)

        batch_records: dict[str, ActionRecord] = {}
        if present_items:
            unit_names = [it.identifier for it in present_items]
            res = executor.run(
                ["systemctl", "--user", "stop", *unit_names], sudo=False, mutating=True
            )
            if res.returncode == 0 or res.dry_run:
                outcomes = [True] * len(present_items)
            elif len(present_items) > 1:
                active_flags = [self.get_status(it, executor).active for it in present_items]
                outcomes = (
                    [not flag for flag in active_flags]
                    if (any(active_flags) and not all(active_flags))
                    else [False] * len(present_items)
                )
            else:
                outcomes = [False]
            for u, ok in zip(unit_names, outcomes, strict=True):
                batch_records[u] = ActionRecord(
                    action="stop",
                    provider=self.provider_name,
                    identifier=u,
                    command=res.command_str,
                    success=ok,
                    message=res.stdout or res.stderr,
                    dry_run=res.dry_run,
                )

        return [
            absent_records[it.identifier]
            if it.identifier in absent_records
            else batch_records[it.identifier]
            for it in items
        ]
