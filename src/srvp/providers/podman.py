"""Podman standalone container and Podman Compose stack providers."""

from __future__ import annotations

from srvp.config import ProfileConfig
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


class PodmanContainerProvider(ServiceProvider):
    """Manages standalone Podman containers via `podman start` / `podman stop`."""

    provider_name = "podman_container"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        return [
            ManagedItem(provider=self.provider_name, identifier=container)
            for container in profile.podman.containers
        ]

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        res = executor.run(
            ["podman", "inspect", "-f", "{{.State.Running}}|{{.State.Pid}}", item.identifier],
            sudo=False,
            mutating=False,
        )
        active = False
        pid: int | None = None
        if res.returncode == 0 and "|" in res.stdout:
            running_str, pid_str = res.stdout.strip().split("|", 1)
            active = running_str.lower() == "true"
            try:
                parsed_pid = int(pid_str)
                if parsed_pid > 0:
                    pid = parsed_pid
            except ValueError:
                pass
        metrics = executor.get_process_metrics(pid) if (active and pid) else None
        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=active,
            status_text="running" if active else "stopped",
            pid=pid,
            cpu_percent=metrics.cpu_percent if metrics else 0.0,
            memory_mb=metrics.memory_mb if metrics else 0.0,
            detail="podman container",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        if not items:
            return []
        names = [it.identifier for it in items]
        res = executor.run(["podman", "start", *names], sudo=False, mutating=True)
        return [
            ActionRecord(
                action="start",
                provider=self.provider_name,
                identifier=name,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for name in names
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        names = [it.identifier for it in items]
        res = executor.run(["podman", "stop", *names], sudo=False, mutating=True)
        return [
            ActionRecord(
                action="stop",
                provider=self.provider_name,
                identifier=name,
                command=res.command_str,
                success=res.returncode == 0,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for name in names
        ]


class PodmanComposeProvider(ServiceProvider):
    """Manages Podman Compose stacks via `podman compose -f <file> up -d / down`."""

    provider_name = "podman_compose"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        items: list[ManagedItem] = []
        for stack in profile.podman.compose_stacks:
            meta: dict[str, str] = {"file": stack.file}
            if stack.project_dir:
                meta["project_dir"] = stack.project_dir
            if stack.services:
                meta["services"] = ",".join(stack.services)
            items.append(
                ManagedItem(
                    provider=self.provider_name,
                    identifier=stack.name,
                    metadata=meta,
                )
            )
        return items

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        compose_file = item.metadata.get("file", "")
        project_dir = item.metadata.get("project_dir")
        res = executor.run(
            ["podman", "compose", "-f", compose_file, "ps", "--status", "running", "-q"],
            cwd=project_dir,
            sudo=False,
            mutating=False,
        )
        active = res.returncode == 0 and bool(res.stdout.strip())
        return ServiceStatus(
            provider=self.provider_name,
            identifier=item.identifier,
            active=active,
            status_text="up" if active else "down",
            pid=None,
            cpu_percent=0.0,
            memory_mb=0.0,
            detail=f"podman compose ({compose_file})",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in items:
            compose_file = item.metadata.get("file", "")
            project_dir = item.metadata.get("project_dir")
            services_csv = item.metadata.get("services", "")
            services = [s for s in services_csv.split(",") if s]
            cmd = ["podman", "compose", "-f", compose_file, "up", "-d", *services]
            res = executor.run(cmd, cwd=project_dir, sudo=False, mutating=True)
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
            compose_file = item.metadata.get("file", "")
            project_dir = item.metadata.get("project_dir")
            cmd = ["podman", "compose", "-f", compose_file, "down"]
            res = executor.run(cmd, cwd=project_dir, sudo=False, mutating=True)
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
