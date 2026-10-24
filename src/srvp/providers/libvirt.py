"""Libvirt / KVM virtual machine provider (`virsh start` / `virsh shutdown`)."""

from __future__ import annotations

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


class LibvirtVmProvider(ServiceProvider):
    """Manages Libvirt/KVM virtual machine domains via `virsh`."""

    provider_name = "libvirt_vm"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        return [
            ManagedItem(provider=self.provider_name, identifier=vm) for vm in profile.libvirt.vms
        ]

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        res = executor.run(
            ["virsh", "domstate", item.identifier],
            sudo=False,
            mutating=False,
        )
        state_str = res.stdout.strip().lower() if res.returncode == 0 else "shut off"
        active = "running" in state_str
        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=active,
            status_text="running" if active else "shut off",
            pid=None,
            cpu_percent=0.0,
            memory_mb=0.0,
            detail="Libvirt/KVM VM",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in items:
            res = executor.run(["virsh", "start", item.identifier], sudo=False, mutating=True)
            records.append(
                ActionRecord(
                    action="start",
                    provider=self.provider_name,
                    identifier=item.identifier,
                    command=res.command_str,
                    success=res.returncode == 0,
                    message=res.stdout or res.stderr,
                    dry_run=res.dry_run,
                )
            )
        return records

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in items:
            res = executor.run(["virsh", "shutdown", item.identifier], sudo=False, mutating=True)
            records.append(
                ActionRecord(
                    action="stop",
                    provider=self.provider_name,
                    identifier=item.identifier,
                    command=res.command_str,
                    success=res.returncode == 0,
                    message=res.stdout or res.stderr,
                    dry_run=res.dry_run,
                )
            )
        return records
