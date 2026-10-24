"""Docker standalone container and Docker Compose stack providers."""

from __future__ import annotations

from srvp.config import ProfileConfig, resolve_compose_paths
from srvp.executor import CommandExecutor
from srvp.providers.base import ActionRecord, ServiceProvider, ServiceStatus
from srvp.state import ManagedItem


class DockerContainerProvider(ServiceProvider):
    """Manages standalone Docker containers via `docker start` / `docker stop`."""

    provider_name = "docker_container"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        return [
            ManagedItem(provider=self.provider_name, identifier=container)
            for container in profile.docker.containers
        ]

    def get_status(self, item: ManagedItem, executor: CommandExecutor) -> ServiceStatus:
        res = executor.run(
            ["docker", "inspect", "-f", "{{.State.Running}}|{{.State.Pid}}", item.identifier],
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
            detail="docker container",
        )

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        if not items:
            return []
        names = [it.identifier for it in items]
        res = executor.run(["docker", "start", *names], sudo=False, mutating=True)
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
                identifier=name,
                command=res.command_str,
                success=ok,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for name, ok in zip(names, outcomes, strict=True)
        ]

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        if not items:
            return []
        names = [it.identifier for it in items]
        res = executor.run(["docker", "stop", *names], sudo=False, mutating=True)
        if res.returncode == 0 or res.dry_run:
            outcomes = [True] * len(items)
        elif len(items) > 1:
            active_flags = [self.get_status(it, executor).active for it in items]
            outcomes = (
                [not flag for flag in active_flags]
                if (any(active_flags) and not all(active_flags))
                else [False] * len(items)
            )
        else:
            outcomes = [False]
        return [
            ActionRecord(
                action="stop",
                provider=self.provider_name,
                identifier=name,
                command=res.command_str,
                success=ok,
                message=res.stdout or res.stderr,
                dry_run=res.dry_run,
            )
            for name, ok in zip(names, outcomes, strict=True)
        ]


class DockerComposeProvider(ServiceProvider):
    """Manages Docker Compose stacks via `docker compose -f <file> up -d / down`."""

    provider_name = "docker_compose"

    def get_desired_items(self, profile: ProfileConfig) -> list[ManagedItem]:
        items: list[ManagedItem] = []
        for stack in profile.docker.compose_stacks:
            resolved_file, resolved_proj = resolve_compose_paths(stack.file, stack.project_dir)
            meta: dict[str, str] = {"file": resolved_file}
            if resolved_proj:
                meta["project_dir"] = resolved_proj
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
        compose_file, project_dir = resolve_compose_paths(
            item.metadata.get("file", ""),
            item.metadata.get("project_dir"),
        )
        services_csv = item.metadata.get("services", "")
        services = [s for s in services_csv.split(",") if s]
        res = executor.run(
            ["docker", "compose", "-f", compose_file, "ps", "--status", "running", "-q", *services],
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
            detail=f"compose ({compose_file})",
        )

    def has_running_containers(self, item: ManagedItem, executor: CommandExecutor) -> bool:
        """Return True if any container belonging to `item`'s Compose file is running."""
        compose_file, project_dir = resolve_compose_paths(
            item.metadata.get("file", ""),
            item.metadata.get("project_dir"),
        )
        res = executor.run(
            ["docker", "compose", "-f", compose_file, "ps", "--status", "running", "-q"],
            cwd=project_dir,
            sudo=False,
            mutating=False,
        )
        return res.returncode == 0 and bool(res.stdout.strip())

    def start_items(
        self, items: list[ManagedItem], executor: CommandExecutor
    ) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in items:
            compose_file, project_dir = resolve_compose_paths(
                item.metadata.get("file", ""),
                item.metadata.get("project_dir"),
            )
            services_csv = item.metadata.get("services", "")
            services = [s for s in services_csv.split(",") if s]
            cmd = ["docker", "compose", "-f", compose_file, "up", "-d", *services]
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
            if res.returncode != 0:
                break
        return records

    def stop_items(self, items: list[ManagedItem], executor: CommandExecutor) -> list[ActionRecord]:
        records: list[ActionRecord] = []
        for item in items:
            compose_file, project_dir = resolve_compose_paths(
                item.metadata.get("file", ""),
                item.metadata.get("project_dir"),
            )
            cmd = ["docker", "compose", "-f", compose_file, "down"]
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
            if res.returncode != 0:
                break
        return records
