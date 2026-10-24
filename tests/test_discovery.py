"""Discovery and automatic snapshot tests."""

from __future__ import annotations

import pytest

from srvp.discovery import build_snapshot_profile, discover_installed_services
from srvp.executor import CommandResult, MockCommandExecutor, ProcessMetrics


def test_snapshot_preserves_compose_container_ownership(
    mock_executor: MockCommandExecutor,
) -> None:
    original_run = mock_executor.run
    compose_containers = (
        "web-1|running|project-a|/tmp/project/compose.yml|/tmp/project|web\n"
        "worker-1|running|project-a|/tmp/project/compose.yml|/tmp/project|worker"
    )

    def report_compose_containers(
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        if cmd[:2] == ["docker", "ps"]:
            return CommandResult(cmd, 0, stdout=compose_containers)
        return original_run(cmd, sudo=sudo, cwd=cwd, shell=shell, mutating=mutating)

    mock_executor.run = report_compose_containers  # type: ignore[method-assign]
    snapshot = build_snapshot_profile(mock_executor)

    assert snapshot.docker.containers == []
    assert len(snapshot.docker.compose_stacks) == 1
    stack = snapshot.docker.compose_stacks[0]
    assert stack.name == "project-a"
    assert stack.file == "/tmp/project/compose.yml"
    assert stack.project_dir == "/tmp/project"
    assert stack.services == ["web", "worker"]


class _DiscoveryExecutor:
    is_dry_run = False

    def __init__(self, *, daemon_active: bool = False) -> None:
        self.daemon_active = daemon_active
        self.commands: list[list[str]] = []

    def run(
        self,
        cmd: list[str],
        *,
        sudo: bool = False,
        cwd: str | None = None,
        shell: bool = False,
        mutating: bool = True,
    ) -> CommandResult:
        self.commands.append(cmd)
        if cmd[:3] == ["docker", "context", "show"]:
            return CommandResult(cmd, 0, stdout="default")
        if cmd[:3] == ["systemctl", "is-active", "--quiet"]:
            return CommandResult(cmd, 0 if self.daemon_active else 3)
        return CommandResult(cmd, 0)

    def get_process_metrics(self, pid: int) -> ProcessMetrics:
        raise NotImplementedError

    def glob_paths(self, pattern: str) -> list[str]:
        return []

    def get_user_groups(self) -> list[str]:
        return []

    def file_exists(self, path: str) -> bool:
        return False


def test_discovery_does_not_connect_to_inactive_local_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    executor = _DiscoveryExecutor()

    discover_installed_services(executor)

    assert not any(cmd[:2] == ["docker", "ps"] for cmd in executor.commands)


def test_discovery_allows_remote_docker_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker.example:2376")
    executor = _DiscoveryExecutor()

    discover_installed_services(executor)

    assert any(cmd[:2] == ["docker", "ps"] for cmd in executor.commands)
