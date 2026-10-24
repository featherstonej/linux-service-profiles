"""Unit tests for all service providers using MockCommandExecutor."""

from __future__ import annotations

from srvp.config import (
    AndroidConfig,
    DockerComposeStack,
    DockerConfig,
    Esp32SerialConfig,
    ProfileConfig,
    SystemdConfig,
)
from srvp.executor import MockCommandExecutor
from srvp.providers import (
    AdbProvider,
    DockerComposeProvider,
    DockerContainerProvider,
    Esp32SerialProvider,
    SystemdSystemProvider,
    SystemdUserProvider,
    run_hook_commands,
)


def test_systemd_system_provider_batched_start_and_stop(
    mock_executor: MockCommandExecutor,
) -> None:
    provider = SystemdSystemProvider()
    profile = ProfileConfig(
        systemd=SystemdConfig(
            system=["postgresql", "redis-server"],
            ensure_stopped=["docker"],
        )
    )
    desired = provider.get_desired_items(profile)
    assert [it.identifier for it in desired] == ["postgresql.service", "redis-server.service"]

    start_records = provider.start_items(desired, mock_executor)
    assert len(start_records) == 2
    assert all(r.success for r in start_records)
    # Verify single batched sudo systemctl start command
    assert mock_executor.history[-1].command == [
        "sudo",
        "systemctl",
        "start",
        "postgresql.service",
        "redis-server.service",
    ]

    status_pg = provider.get_status(desired[0], mock_executor)
    assert status_pg.active is True
    assert status_pg.pid is not None and status_pg.pid > 0
    assert status_pg.memory_mb > 0.0

    stop_records = provider.stop_items(desired, mock_executor)
    assert len(stop_records) == 2
    assert provider.get_status(desired[0], mock_executor).active is False


def test_systemd_user_provider(mock_executor: MockCommandExecutor) -> None:
    provider = SystemdUserProvider()
    profile = ProfileConfig(systemd=SystemdConfig(user=["syncthing"]))
    items = provider.get_desired_items(profile)
    assert len(items) == 1
    assert items[0].identifier == "syncthing.service"

    provider.start_items(items, mock_executor)
    assert mock_executor.history[-1].command == [
        "systemctl",
        "--user",
        "start",
        "syncthing.service",
    ]
    st = provider.get_status(items[0], mock_executor)
    assert st.active is True

    provider.stop_items(items, mock_executor)
    assert provider.get_status(items[0], mock_executor).active is False


def test_docker_container_and_compose_providers(mock_executor: MockCommandExecutor) -> None:
    c_prov = DockerContainerProvider()
    comp_prov = DockerComposeProvider()
    profile = ProfileConfig(
        docker=DockerConfig(
            enabled=True,
            containers=["dev-postgres", "dev-redis"],
            compose_stacks=[
                DockerComposeStack(
                    name="backend",
                    file="/tmp/compose.yml",
                    project_dir="/tmp",
                    services=["api", "worker"],
                )
            ],
        )
    )

    c_items = c_prov.get_desired_items(profile)
    c_prov.start_items(c_items, mock_executor)
    assert c_prov.get_status(c_items[0], mock_executor).active is True
    c_prov.stop_items(c_items, mock_executor)
    assert c_prov.get_status(c_items[0], mock_executor).active is False

    comp_items = comp_prov.get_desired_items(profile)
    comp_prov.start_items(comp_items, mock_executor)
    assert comp_prov.get_status(comp_items[0], mock_executor).active is True
    comp_prov.stop_items(comp_items, mock_executor)
    assert comp_prov.get_status(comp_items[0], mock_executor).active is False


def test_adb_provider(mock_executor: MockCommandExecutor) -> None:
    prov = AdbProvider()
    profile = ProfileConfig(android=AndroidConfig(adb_server=True))
    items = prov.get_desired_items(profile)
    assert len(items) == 1

    prov.start_items(items, mock_executor)
    st = prov.get_status(items[0], mock_executor)
    assert st.active is True
    assert st.pid == 4242

    prov.stop_items(items, mock_executor)
    assert prov.get_status(items[0], mock_executor).active is False


def test_esp32_serial_provider_and_hooks(mock_executor: MockCommandExecutor) -> None:
    prov = Esp32SerialProvider()
    profile = ProfileConfig(esp32_serial=Esp32SerialConfig(enabled=True))
    items = prov.get_desired_items(profile)
    assert len(items) == 1

    records = prov.start_items(items, mock_executor)
    assert len(records) == 1
    assert "/dev/ttyUSB0" in records[0].message
    assert "dialout OK" in records[0].message

    # Verify Arch Linux / Manjaro 'uucp' serial group support
    mock_executor.user_groups = ["james", "wheel", "uucp"]
    uucp_status = prov.get_status(items[0], mock_executor)
    assert uucp_status.active is True
    assert "uucp OK" in uucp_status.detail

    hook_recs = run_hook_commands("post_start", ["echo hello"], mock_executor)
    assert len(hook_recs) == 1
    assert hook_recs[0].success is True
