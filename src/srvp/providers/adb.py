"""Android Debug Bridge (ADB) server provider."""

from __future__ import annotations

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


class AdbProvider(ServiceProvider):
    """Manages the Android Debug Bridge daemon (`adb start-server` / `adb kill-server`)."""

    provider_name = "adb"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        if profile.android.adb_server:
            return [ManagedItem(provider=self.provider_name, identifier="adb-server")]
        return []

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        pgrep_res = executor.run(["pgrep", "-x", "adb"], sudo=False, mutating=False)
        active = pgrep_res.returncode == 0 and bool(pgrep_res.stdout.strip())
        pid: int | None = None
        if active:
            first_line = pgrep_res.stdout.strip().splitlines()[0]
            try:
                pid = int(first_line)
            except ValueError:
                pid = None
        metrics = executor.get_process_metrics(pid) if (active and pid) else None
        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=active,
            status_text="running" if active else "stopped",
            pid=pid,
            cpu_percent=metrics.cpu_percent if metrics else 0.0,
            memory_mb=metrics.memory_mb if metrics else 0.0,
            detail="Android ADB daemon",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        if not items:
            return []
        res = executor.run(["adb", "start-server"], sudo=False, mutating=True)
        return [
            ActionRecord(
                action="start",
                provider=self.provider_name,
                identifier=it.identifier,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for it in items
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        res = executor.run(["adb", "kill-server"], sudo=False, mutating=True)
        return [
            ActionRecord(
                action="stop",
                provider=self.provider_name,
                identifier=it.identifier,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for it in items
        ]
